# -*- coding: utf-8 -*-
"""6 步启动 check 函数（每步独立、内部 try/except 返 CheckResult、绝不抛）。

启动依赖链（与原 on_startup 一致，顺序仅挪位置）：
  data_dir → database + settings → scheduler（cleanup 都放 scheduler 末尾）
  ffmpeg / playwright 独立

每步函数职责见模块顶部文档，调用方为 startup_state.check(key)。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from loguru import logger

from app.core import startup_state
from app.core.logger import init_logger_file  # 模块顶部 import，避免散在 try 内
from app.core.startup_state import CheckResult


# ---------- 步骤 2：数据目录 ----------

def check_data_dir() -> CheckResult:
    """应用目录 + 数据目录初始化，data_dir 就绪后挂 loguru 文件 sink。

    检测顺序（#597 重构：默认盘探测改在主进程 ensureDataDirChoice 弹窗内）：
    1. ① env 透传：SHORTVIDEO_DATA_DIR（仅 RUNTIME=1 时信任，避免测试/CI 污染）
    2. ② settings.json 持久化：用户此前选过的 data_dir 字段
    3. 都没配置 / 路径失效 → failed + data.need_choose=true，触发启动页自动调主进程弹窗
       （主进程 ensureDataDirChoice 跑默认盘探测 + 让用户选）

    依赖：无
    失败：路径不可写 / mkdir 失败 / 应用目录不可写 / 都没配置（need_choose）
          / 路径失效（盘符卸载 / 权限被改）—— 也返 need_choose 让用户重选
    """
    try:
        from app.services.setting_service import (
            init_app_dirs,
            init_data_dir,
            get_data_dir,
            load_settings,
        )
        init_app_dirs()
        # ① env 透传（仅 RUNTIME=1 时信任；统一 strip 防御 env 含不可见字符）
        env_arg: str | None = None
        if os.environ.get("SHORTVIDEO_TOOL_RUNTIME"):
            raw = os.environ.get("SHORTVIDEO_DATA_DIR", "").strip()
            if raw:
                env_arg = raw
        # ② settings.json 持久化（用户此前选过；init_app_dirs 后 load_settings 可用）
        settings_arg: str | None = None
        try:
            sd = load_settings().get("data_dir")
            if isinstance(sd, str) and sd.strip():
                settings_arg = sd.strip()
        except Exception:  # noqa: BLE001 配置损坏用空（DEFAULT_SETTINGS 兜底）
            pass
        # 优先级：env > settings.json
        chosen = env_arg or settings_arg
        if not chosen:
            # ③ 都没配置 → 通知前端触发主进程 ensureDataDirChoice 弹窗
            return CheckResult(
                key="data_dir",
                status="failed",
                detail="未配置数据目录，请选择数据目录",
                data={"need_choose": True},
            )
        # 路径失效（盘符卸载 / 权限被改）也会抛 RuntimeError/Exception，
        # 外层 except 捕获后也设 need_choose=true → 启动页给用户重选机会
        init_data_dir(Path(chosen))
        # 数据目录就绪 → 挂文件 sink（init_logger_file 内部调 get_data_dir()）
        init_logger_file()
        data_dir = get_data_dir()
        return CheckResult(
            key="data_dir",
            status="ok",
            detail=f"数据目录: {data_dir}",
            data={"data_dir": str(data_dir)},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="data_dir",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
            # #597：路径失效也允许用户重选（不仅是"没配置"）
            data={"need_choose": True},
        )


# ---------- 步骤 3：数据库 + 迁移 ----------

def check_database() -> CheckResult:
    """DB 连接 + migrate + 3 个数据迁移。

    依赖：data_dir（DB 路径）
    失败：DB 连接 / DDL / 数据迁移任意一步失败
    """
    try:
        from app.db import init_db, get_db
        from app.db.migrations import migrate
        from app.services.setting_service import get_data_dir

        # init_db 内部已判断幂等（重复调不重连）
        init_db(get_data_dir() / "db" / "short_video_tools.db")
        db = get_db()
        version = migrate(db)
        # 4.1 历史空串 category_id → 未分类虚拟 ID（一次性）
        from app.services.material_service import migrate_empty_category_to_uncategorized
        migrated_cat = migrate_empty_category_to_uncategorized(db)
        if migrated_cat:
            logger.info("[启动迁移] category_id='' → '-'：{} 条", migrated_cat)
        # 4.2 #363：项目片段路径 project/<pid>/...
        from app.services.creation_service import migrate_clip_paths_to_project
        clip_mig = migrate_clip_paths_to_project()
        if clip_mig.get("moved_files") or clip_mig.get("updated_rows"):
            logger.info(
                "[启动迁移] 片段路径 project/ 化：搬移 {} 个文件，更新 {} 行",
                clip_mig.get("moved_files"), clip_mig.get("updated_rows"),
            )
        # 4.3 #364：素材按 id 子目录聚合
        from app.services.material_service import migrate_material_paths_to_id_subdir
        mat_mig = migrate_material_paths_to_id_subdir()
        if mat_mig.get("moved_files") or mat_mig.get("updated_rows"):
            logger.info(
                "[启动迁移] 素材按 id 子目录化：搬移 {} 个文件，更新 {} 行",
                mat_mig.get("moved_files"), mat_mig.get("updated_rows"),
            )
        return CheckResult(
            key="database",
            status="ok",
            detail=f"数据库已迁移到 v{version}",
            data={"version": version, "category_migrated": migrated_cat},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="database",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )


# ---------- 步骤 4：系统配置 ----------

def check_settings() -> CheckResult:
    """加密密钥 + load_settings。

    依赖：data_dir（APP_CONFIG_DIR）
    失败：密钥文件读写失败 / settings.json 损坏（损坏已用默认值兜底，不会失败）
    """
    try:
        from app.core import crypto
        from app.services import setting_service
        from app.services.setting_service import get_config_dir

        crypto.init_crypto(get_config_dir())
        settings = setting_service.load_settings()
        # 仅展示已加载的关键字段（不暴露密钥/账号等敏感字段）
        # 加白名单前确认不暴露密钥/账号/token 等敏感信息
        public_keys = (
            "log_retention_days", "temp_retention_days", "browser_show_window",
            "account_check_hours",
        )
        loaded = {k: settings.get(k) for k in public_keys if k in settings}
        # data_dir 不从 settings 读（reset 后可能还是旧值），直接走 get_data_dir() 取真实生效路径
        loaded["data_dir"] = str(setting_service.get_data_dir())
        return CheckResult(
            key="settings",
            status="ok",
            detail=f"已加载 {len(settings)} 项配置",
            data={"loaded": loaded},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="settings",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )


# ---------- 步骤 5：ffmpeg ----------

def check_ffmpeg() -> CheckResult:
    """ffmpeg 可执行 + version + libx264 实测。

    依赖：无
    失败：未找到 / 无法执行 / 不支持 libx264
    """
    try:
        from app.core.ffmpeg import FFMPEG, run_cmd

        # 1. ffmpeg 可执行文件存在
        if not shutil.which(FFMPEG) and not Path(FFMPEG).exists():
            return CheckResult(
                key="ffmpeg",
                status="failed",
                detail=f"未找到 ffmpeg 可执行文件: {FFMPEG}",
                data={"step": "which"},
            )

        # 2. ffmpeg 能跑起来
        ver = run_cmd([FFMPEG, "-hide_banner", "-version"], timeout=10)
        if ver is None or ver.returncode != 0:
            rc = ver.returncode if ver else "None"
            return CheckResult(
                key="ffmpeg",
                status="failed",
                detail=f"ffmpeg 无法执行 (rc={rc})",
                data={"step": "version"},
            )

        # 3. libx264 编码器实测：1 帧 64x64 黑盒
        test_cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04",
            "-c:v", "libx264", "-frames:v", "1", "-f", "null", "-",
        ]
        test = run_cmd(test_cmd, timeout=10)
        if test is None or test.returncode != 0:
            return CheckResult(
                key="ffmpeg",
                status="failed",
                detail="ffmpeg 不支持 libx264 编码器",
                data={"step": "libx264"},
            )

        # 解析 ffmpeg 版本号（首行 'ffmpeg version X.Y.Z ...'）
        version_str = ""
        if ver.stdout:
            first_line = ver.stdout.strip().splitlines()[0] if ver.stdout.strip() else ""
            parts = first_line.split()
            if len(parts) >= 3:
                version_str = parts[2]

        return CheckResult(
            key="ffmpeg",
            status="ok",
            detail=f"ffmpeg + libx264 实测通过 (ffmpeg {version_str})",
            data={"ffmpeg_version": version_str},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="ffmpeg",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )


# ---------- 步骤 6：ms-playwright ----------

def _chrome_exe_candidates(latest: Path) -> list[Path]:
    """按平台返回 chrome 可执行路径候选列表（优先新版 playwright 1.50+ 的 -64 后缀布局）。

    playwright 1.50+ 目录布局变更：
      win-x64:    chromium-*/chrome-win64/chrome.exe（旧版 chrome-win/chrome.exe）
      mac-x64:    chromium-*/chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium（旧 chrome-mac）
      mac-arm64:  chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium
      linux-x64:  chromium-*/chrome-linux64/chrome（旧 chrome-linux/chrome）

    候选顺序：新布局 → 老布局，第一个存在即胜出。
    """
    if sys.platform == "win32":
        return [
            latest / "chrome-win64" / "chrome.exe",
            latest / "chrome-win" / "chrome.exe",
        ]
    if sys.platform == "darwin":
        # arm64 优先（Apple Silicon 自家机器一般走 arm64；x64 在 Intel Mac / Rosetta）
        return [
            latest / "chrome-mac-arm64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            latest / "chrome-mac-x64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            latest / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
        ]
    # linux
    return [
        latest / "chrome-linux64" / "chrome",
        latest / "chrome-linux" / "chrome",
    ]


def _ms_playwright_root_candidates() -> list[Path]:
    """返回 ms-playwright 根目录候选列表（按优先级）。

    优先级：
    1. PLAYWRIGHT_BROWSERS_PATH 环境变量（生产 main.ts spawn 时显式设置）
    2. <exe 父目录>/assets/ms-playwright（生产 PyInstaller 包布局）
    3. <cwd>/assets/ms-playwright（dev 模式 cwd = backend/）
    4. <cwd>/../assets/ms-playwright（dev 模式 cwd = backend/app/）
    5. %LOCALAPPDATA%/ms-playwright（playwright 默认安装位置）
    """
    cands: list[Path] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path:
        cands.append(Path(env_path))
    try:
        # sys.executable 在 frozen 模式下是 exe 路径；dev 模式是 python.exe 路径
        exe_parent = Path(sys.executable).resolve().parent
        cands.append(exe_parent / "assets" / "ms-playwright")
        # frozen 时 sys._MEIPASS 指向 _internal/，其 parent = exe 同级
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass).resolve().parent / "assets" / "ms-playwright")
    except OSError:
        pass
    try:
        cwd = Path.cwd().resolve()
        cands.append(cwd / "assets" / "ms-playwright")
        # dev 模式 uvicorn 从 backend/ 启动（cwd=backend），但 pytest / 嵌套调用 cwd 可能是 backend/app
        cands.append(cwd.parent / "assets" / "ms-playwright")
    except OSError:
        pass
    # playwright 默认安装位置
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            cands.append(Path(local_app_data) / "ms-playwright")
    elif sys.platform == "darwin":
        cands.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        cands.append(Path.home() / ".cache" / "ms-playwright")
    return cands


def check_playwright() -> CheckResult:
    """Chromium 可执行路径探测（不实际 launch，避免占资源 + 10s 等待）。

    依赖：无
    失败：所有候选路径都找不到 Chromium
    """
    try:
        # 多候选探测：env → exe 旁 → cwd 旁 → playwright 默认
        tried: list[str] = []
        for pw_root in _ms_playwright_root_candidates():
            tried.append(str(pw_root))
            if not pw_root.exists() or not pw_root.is_dir():
                continue

            # 找 chromium-* 子目录（按版本号整数大小取最新，避免字典序 "1187" < "1234" 但 "999" < "1000" 的歧义）
            chromium_dirs = [d for d in pw_root.glob("chromium-*") if d.is_dir()]
            if not chromium_dirs:
                continue

            def _rev_num(p: Path) -> int:
                try:
                    return int(p.name.split("-", 1)[1])
                except (IndexError, ValueError):
                    return -1

            latest = max(chromium_dirs, key=_rev_num)

            for candidate in _chrome_exe_candidates(latest):
                if candidate.exists():
                    return CheckResult(
                        key="playwright",
                        status="ok",
                        detail=f"Chromium 路径可用: {candidate}",
                        data={
                            "chromium_path": str(candidate),
                            "chromium_dir": str(latest),
                            "pw_browsers_path": str(pw_root),
                        },
                    )

        # 所有候选都失败
        return CheckResult(
            key="playwright",
            status="failed",
            detail=f"未找到 Chromium（已探测 {len(tried)} 个路径）",
            data={"step": "all_candidates", "tried": tried},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="playwright",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )


# ---------- 步骤 7：调度服务 ----------

def check_scheduler() -> CheckResult:
    """start_scheduler + register_account_check_job + register_enabled_tasks + cleanup。

    依赖：database（register_enabled_tasks 查 shop_pull_task / video_pull_task 表）
          settings（register_account_check_job 读 account_check_hours）
    失败：任一注册失败 / cleanup 不抛但记 warning
    """
    try:
        from app.core import task_scheduler as ts
        from app.services import account_service, selection_service, material_service
        from app.services import setting_service
        from app.core import notifier

        ts.start_scheduler()
        # 登录态定时检测
        account_service.register_account_check_job()
        # enabled 拉取任务恢复（M3 提供 register_shop_pull / register_task_job）
        # 模块未实现时返 ok + skip 标记，让 UI 知道"调度器启动了但部分任务跳过"
        skipped: list[str] = []
        try:
            ts.register_enabled_tasks(
                register_shop_pull=selection_service.register_shop_pull,
                register_video_pull=material_service.register_task_job,
            )
        except ImportError as exc:
            skipped.append(f"enabled_tasks 跳过（模块未实现: {exc}）")

        # cleanup 都放最后（与原 on_startup 语义一致）
        settings = setting_service.load_settings()
        log_retention = settings.get("log_retention_days", 30)
        temp_retention = settings.get("temp_retention_days", 7)
        cleaned_notifications = notifier.cleanup_expired(log_retention)
        setting_service.cleanup_startup_temp(temp_retention)

        # 有 skip 仍算 ok（scheduler 主流程已起来），detail 注明跳过项
        detail = f"调度器已启动，通知清理 {cleaned_notifications} 条"
        if skipped:
            detail += f"；{'；'.join(skipped)}"

        return CheckResult(
            key="scheduler",
            status="ok",
            detail=detail,
            data={"cleaned_notifications": cleaned_notifications, "skipped": skipped},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            key="scheduler",
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
        )


# 步骤注册表（6 步；后端存活由前端 Electron 主进程 waitForBackendReady 判定，不在此表）
STEPS = [
    ("data_dir",   check_data_dir),
    ("database",   check_database),
    ("settings",   check_settings),
    ("ffmpeg",     check_ffmpeg),
    ("playwright", check_playwright),
    ("scheduler",  check_scheduler),
]


# 模块加载时立即注册到 startup_state（不在 on_startup 阶段）。
# 目的：保证后端 Python 进程 import 完 → STEPS 已注册 → uvicorn listen 后
# /api/startup/check/{key} 立即可用，根除 on_startup 异步触发期间
# 前端 health 已通 + check 接口返回 404 的 race condition。
# register 幂等（重复注册覆盖 runner，不重置 state），与 on_startup 调效果一致。
for _key, _fn in STEPS:
    startup_state.register(_key, _fn)
