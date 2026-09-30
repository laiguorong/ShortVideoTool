# -*- coding: utf-8 -*-
r"""系统设置服务（F-08）：data 目录管理、全局配置、健康检查、备份恢复。

目录策略（#361）：
- **应用目录（不可变更）**：随程序所在目录，仅放程序自身需要的家目录
  - config/   —— 全局配置 settings.json（首次写入后固定，不应跟随数据目录漂移）
  - models/   —— AI 模型（人脸检测模型等，下载到本地后属于应用级资源）
- **本地数据目录（用户可变更路径）**：业务数据落地
  - 临时（可清理）—— temp / cache / log
  - 永久（业务功能删除）—— db / material / create / finished

首次启动：按可用空间最大的固定磁盘根下创建 ShortVideoToolData，
写入 config.data_dir 字段（应用目录的 config/settings.json），之后沿用不漂移。
"""

import json
import os
import shutil
import string
import zipfile
from datetime import datetime
from pathlib import Path

from app.db import get_db
from app.db.utils import now_str

# ---------------- 目录全局变量（启动时 init_* 设置） ----------------

# 应用目录（不可变更；cwd 即 main.py 启动目录）
APP_DIR: Path | None = None
APP_CONFIG_DIR: Path | None = None
APP_MODELS_DIR: Path | None = None

# 本地数据目录（用户可改；首次自动选最大盘根下 ShortVideoToolData）
DATA_DIR: Path | None = None

# 数据子目录分组（#361）
DATA_TEMP_SUBDIRS = ["temp", "cache", "log"]                  # 可清理（手动 + 启动按 retention_days）
DATA_PERM_SUBDIRS = ["db", "material", "create", "finished"]  # 永久：业务功能删除
DATA_SUB_DIRS = DATA_TEMP_SUBDIRS + DATA_PERM_SUBDIRS

# 数据目录默认文件夹名（落在所选盘根下，如 E:\ShortVideoToolData）
_DEFAULT_DATA_DIR_NAME = "ShortVideoToolData"

# 全局配置默认值（应用目录 config/settings.json，需求文档 F-08.4）
DEFAULT_SETTINGS = {
    "account_check_hours": 6,                # 账号登录态检测周期（1~24）
    "browser_show_window": False,            # #410 调试开关：是否显示浏览器窗口（默认 False 无头）
                                             # 仅影响 run/new_session/补抓身份；登录窗固定显示
    "log_retention_days": 30,
    "temp_retention_days": 7,
    "data_dir": "",                          # 数据目录绝对路径；空 = 首次启动自动选定
    "pull_base_pages": 100,                  # v22 阶梯式翻页：第 1 轮最大页数（线性递减，10 兜底）
    "pull_debug": False,                     # 拉取任务慢路径开关：True 时筛选面板每步 2.5s 便于真机观察
    "task_history_max_count": 500,           # #高危-1：内存中保留的终态任务上限（防 OOM）
    "task_history_retention_hours": 24,      # #高危-1：终态任务保留时长（超时即淘汰）
    # #418：成品视频目录对话框记住的上次选择路径
    "last_video_dir": "",
}


# ---------------- 应用目录（#361 不可变更） ----------------

def get_app_dir() -> Path:
    """应用目录（main.py 启动时的 cwd；启动后不变）。"""
    if APP_DIR is None:
        return Path.cwd()
    return APP_DIR


def init_app_dirs() -> Path:
    """初始化应用目录：建 config/ 与 models/（幂等）。"""
    global APP_DIR, APP_CONFIG_DIR, APP_MODELS_DIR
    APP_DIR = Path.cwd()
    APP_CONFIG_DIR = APP_DIR / "config"
    APP_MODELS_DIR = APP_DIR / "models"
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (APP_MODELS_DIR / "face").mkdir(parents=True, exist_ok=True)
    return APP_DIR


def get_config_dir() -> Path:
    """应用目录 config/（配置与密钥的家，不随数据目录漂移）。"""
    if APP_CONFIG_DIR is None:
        init_app_dirs()
    assert APP_CONFIG_DIR is not None
    return APP_CONFIG_DIR


def get_models_dir() -> Path:
    """应用目录 models/（AI 模型落地；不随数据目录漂移）。"""
    if APP_MODELS_DIR is None:
        init_app_dirs()
    assert APP_MODELS_DIR is not None
    return APP_MODELS_DIR


def pick_largest_disk() -> Path:
    """挑可用空间最大的固定磁盘（Windows 盘符枚举；排除光驱/U盘/网络盘）。

    返回:
        该盘根路径（如 E:\）；枚举失败回退当前工作目录所在盘。
    """
    best: tuple[int, Path] | None = None
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        # DRIVE_FIXED=3：本地硬盘（排除可移动 2 / 网络 4 / 光驱 5）
        if not os.path.exists(root):
            continue
        try:
            import ctypes
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
            if drive_type != 3:
                continue
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if best is None or free > best[0]:
            best = (free, Path(root))
    return best[1] if best else Path.cwd().anchor


def _looks_like_pollution(path_str: str) -> bool:
    """检测路径是否像测试 fixture 残留（Temp 目录下以 dt_ / test_ / tmp 前缀的子目录）。

    背景：早期 init_data_dir 显式参数会写 settings.json::data_dir，测试 fixture
    init_data_dir(tmp/"data") 把 Temp/dt_xxx/data 持久化到配置；后续 dev 启动
    resolve_default_data_dir() 沿用 → 用户看到的是 Temp 目录。

    已被 init_data_dir 撤回 save_settings 修掉；本函数是兜底防御，扫历史污染。
    """
    from tempfile import gettempdir
    try:
        p = Path(path_str).resolve()
        tmp = Path(gettempdir()).resolve()
        # 必须在 Temp 下
        try:
            p.relative_to(tmp)
        except ValueError:
            return False
        # 父目录以 dt_ / test_ / tmp 前缀开头（pytest mkdtemp / tempfile.mkdtemp 默认前缀）
        parent_name = p.parent.name if p.name == "data" else p.name
        return any(parent_name.startswith(prefix) for prefix in ("dt_", "test_", "tmp"))
    except OSError:
        return False


def resolve_default_data_dir() -> Path:
    """确定默认数据目录：配置已记录则沿用；否则挑最大可用空间盘的 ShortVideoToolData。

    首次选定的目录写回配置——之后插拔盘/空间变化都不再漂移。
    """
    recorded = load_settings().get("data_dir") or ""
    # #data-dir-defense：检测历史污染（Temp/test 残留）→ 强制兜底
    # 即使路径存在（pytest 残留目录仍在），特征匹配也走兜底 + 覆盖 settings.json
    if recorded and Path(recorded).exists() and not _looks_like_pollution(recorded):
        return Path(recorded)
    if recorded:
        from loguru import logger
        if Path(recorded).exists():
            logger.warning(
                "[setting_service] 已记录 data_dir 命中历史污染特征（Temp/test 残留）：{}；强制兜底到最大盘",
                recorded,
            )
        else:
            logger.warning(
                "[setting_service] 已记录数据目录不可达：{}；临时兜底到最大盘，下方覆盖 settings.json",
                recorded,
            )
    data_dir = pick_largest_disk() / _DEFAULT_DATA_DIR_NAME
    save_settings({"data_dir": str(data_dir)})
    return data_dir


def init_data_dir(data_dir: Path | None = None) -> Path:
    """初始化本地数据目录（幂等）：建临时 + 永久子目录，跑老版本迁移。

    参数:
        data_dir: 显式指定（--data-dir / SHORTVIDEO_DATA_DIR env / 测试用）；
                  None = 按默认策略解析（首次启动挑最大盘根 + 落 settings.json）
    """
    global DATA_DIR
    if data_dir is None:
        data_dir = resolve_default_data_dir()
    # 显式参数场景不写 settings.json：
    #  - 测试 fixture init_data_dir(tmp/"data") 会污染 settings.json 指向临时目录
    #  - 前端选择持久化由 spawn env 透传保证（每次主进程启动从 userData JSON 读 → env → 后端）
    DATA_DIR = data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # 路径不可写（如 H 盘只读 / 权限不足 / 盘符已卸载），转统一异常格式
        # 让上层能给出可读错误信息（类似 check_ffmpeg_env 的 RuntimeError 风格）
        raise RuntimeError(
            f"数据目录不可用：{data_dir}（{e.strerror or e}）"
        ) from e
    for sub in DATA_SUB_DIRS:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    # #361：老版本 data_dir/config + data_dir/models/face 迁到应用目录
    _migrate_legacy_app_files()
    return data_dir


def _migrate_legacy_app_files() -> None:
    """#361 一次性迁移：把老版本留在数据目录里的 config/* 与 models/face/* 移到应用目录。

    源文件已迁过（app 目录已有同名文件）则跳过；老空目录保留不动（避免误判用户数据）。
    """
    if DATA_DIR is None or APP_CONFIG_DIR is None or APP_MODELS_DIR is None:
        return
    # 1) config/* → APP_CONFIG_DIR/*
    legacy_config = DATA_DIR / "config"
    if legacy_config.exists() and legacy_config.resolve() != APP_CONFIG_DIR.resolve():
        for f in legacy_config.iterdir():
            if f.is_file() and f.name != ".key":  # .key 留原位（路径敏感性）
                target = APP_CONFIG_DIR / f.name
                if not target.exists():
                    try:
                        shutil.move(str(f), str(target))
                    except OSError:
                        pass
    # 2) models/face/* → APP_MODELS_DIR/face/*
    legacy_models_face = DATA_DIR / "models" / "face"
    if legacy_models_face.exists() and legacy_models_face.resolve() != (APP_MODELS_DIR / "face").resolve():
        for f in legacy_models_face.rglob("*"):
            if f.is_file():
                rel = f.relative_to(legacy_models_face)
                target = APP_MODELS_DIR / "face" / rel
                if not target.exists():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(target))
                    except OSError:
                        pass


def get_data_dir() -> Path:
    """获取 data 根目录。"""
    if DATA_DIR is None:
        raise RuntimeError("data 目录未初始化")
    return DATA_DIR


def get_settings_path() -> Path:
    """配置文件路径（程序目录 config/settings.json，不随数据目录漂移）。"""
    return get_config_dir() / "settings.json"


def load_settings() -> dict:
    """读取配置（缺失项用默认值补齐；丢弃未知字段，#412 兼容旧 h264_encoder 等）。"""
    settings = dict(DEFAULT_SETTINGS)
    path = get_settings_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}  # 配置损坏时用默认值
        # 仅合入已知字段，避免旧版本字段（已下线）残留
        for k, v in raw.items():
            if k in DEFAULT_SETTINGS:
                settings[k] = v
    return settings


def save_settings(updates: dict) -> dict:
    """合并保存配置（修改前自动留 .bak），返回保存后的完整配置。"""
    path = get_settings_path()
    current = load_settings()
    merged = {**current, **updates}
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


# ---------- 健康检查（F-08.2） ----------

def health_check() -> dict:
    """目录健康检查：子目录齐全性、db 可写、磁盘剩余空间。"""
    data_dir = get_data_dir()
    items = []
    for sub in DATA_SUB_DIRS:
        p = data_dir / sub
        ok = p.exists() and p.is_dir()
        writable = False
        if ok:
            try:
                (p / ".write_test").write_text("t", encoding="utf-8")
                (p / ".write_test").unlink()
                writable = True
            except OSError:
                pass
        items.append({"dir": sub, "exists": ok, "writable": writable})
    # 磁盘剩余
    free_gb = shutil.disk_usage(data_dir).free / (1024 ** 3)
    all_ok = all(i["exists"] and i["writable"] for i in items)
    return {
        "healthy": all_ok and free_gb > 0.5,
        "items": items,
        "free_gb": round(free_gb, 2),
        "warnings": [
            *(["磁盘剩余不足 500MB，生成类任务将被拒绝"] if free_gb < 0.5
              else ["磁盘剩余不足 2GB，建议清理"] if free_gb < 2 else []),
        ],
    }


# ---------- 缓存与临时清理（F-08.5 / #361：只清临时组） ----------

def cleanup_temp_cache() -> dict:
    """清理 temp + cache + log（#361 临时组），不触及 material/create/finished/db。

    Windows 上 loguru 仍持有今天的日志句柄，unlink 会 PermissionError。
    log 目录跳过当天日志（按文件名 app_YYYYMMDD.log 与今天日期比对），
    其他目录所有 unlink 都包 try 容错，失败累加返回给前端提示。
    """
    data_dir = get_data_dir()
    freed = 0
    counts = 0
    failed = 0
    today_str = datetime.now().strftime("%Y%m%d")
    for sub in DATA_TEMP_SUBDIRS:
        d = data_dir / sub
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            # log 目录：跳过当天正在写入的日志文件
            if sub == "log" and today_str in f.name:
                continue
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
                counts += 1
            except OSError:
                # Windows 文件句柄占用（loguru 当前日志）/ 权限不足，跳过
                failed += 1
        # 清空后重建空目录结构
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return {
        "deleted_files": counts,
        "failed_files": failed,
        "freed_mb": round(freed / 1024 / 1024, 2),
    }


def cleanup_startup_temp(retention_days: int = 7) -> None:
    """启动时清理：残留 temp + 超期缓存（log 不在此处清，按 log_retention_days 单独处理）。"""
    data_dir = get_data_dir()
    deadline = datetime.now().timestamp() - retention_days * 86400
    for sub in ("temp", "cache"):
        d = data_dir / sub
        if not d.exists():
            continue
        for f in d.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < deadline:
                    f.unlink()
            except OSError:
                pass


# ---------- 备份恢复（F-08.7 / F-08-R6 / #361：跨应用目录与数据目录） ----------

def backup(target_path: str, include_material: bool = False, include_finished: bool = False) -> dict:
    """备份：应用目录 config + 数据目录 db/material/create/finished（可选素材/成品）到 zip。

    参数:
        target_path: 备份 zip 输出路径
        include_material / include_finished: 是否包含素材与成品文件（体积大）
    返回:
        {"file": 路径, "size_mb": 大小}
    """
    data_dir = get_data_dir()
    # 先确保 WAL 落盘
    get_db().execute("PRAGMA wal_checkpoint(TRUNCATE);")
    zip_path = Path(target_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # zip 内路径分两类：app/<rel>（应用目录）与 data/<rel>（数据目录）
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) 应用目录 config/settings.json（密钥 .key 不进备份）
        cfg = get_config_dir()
        if cfg.exists():
            for f in cfg.iterdir():
                if f.is_file() and f.name != ".key":
                    zf.write(f, f"app/config/{f.name}")
        # 2) 数据目录永久组 + 可选素材/成品
        perm_subs = list(DATA_PERM_SUBDIRS)
        if not include_material:
            perm_subs.remove("material")
        if not include_finished:
            perm_subs.remove("finished")
        for sub in perm_subs:
            src = data_dir / sub
            if not src.exists():
                continue
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f"data/{sub}/{f.relative_to(src)}")
    return {"file": str(zip_path), "size_mb": round(zip_path.stat().st_size / 1024 / 1024, 2)}


def restore(zip_path: str) -> dict:
    """从备份恢复（zip 内 path 前缀 app/ → 应用目录；data/ → 数据目录；恢复前自动留当前状态快照）。"""
    data_dir = get_data_dir()
    app_cfg = get_config_dir()
    src = Path(zip_path)
    if not src.exists():
        raise FileNotFoundError(f"备份文件不存在：{zip_path}")
    # 当前状态快照（失败回滚依据；放应用目录 config/.pre_restore_*.zip）
    snap = app_cfg / f".pre_restore_{now_str().replace(':', '').replace(' ', '_')}.zip"
    with zipfile.ZipFile(snap, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) 应用目录 config（除 .key 与本次快照）
        if app_cfg.exists():
            for f in app_cfg.iterdir():
                if f.is_file() and f.name != ".key" and not f.name.startswith(".pre_restore"):
                    zf.write(f, f"app/config/{f.name}")
        # 2) 数据目录永久组快照
        for sub in DATA_PERM_SUBDIRS:
            d = data_dir / sub
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.is_file():
                    zf.write(f, f"data/{sub}/{f.relative_to(d)}")
    # 解压分发：按 zip 路径前缀决定目标根（去前缀避免多一层目录）
    count = 0
    with zipfile.ZipFile(src) as zf:
        for name in zf.namelist():
            if name.startswith("app/"):
                rel = name[len("app/"):]  # "config/settings.json"
                target = APP_DIR / rel if APP_DIR is not None else app_cfg.parent / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src_f, open(target, "wb") as dst_f:
                    dst_f.write(src_f.read())
                count += 1
            elif name.startswith("data/"):
                rel = name[len("data/"):]  # "db/short_video_tools.db" 等
                target = data_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src_f, open(target, "wb") as dst_f:
                    dst_f.write(src_f.read())
                count += 1
    return {"restored_files": count}


# ---------- 首次启动状态（风险告知，F-08.6） ----------

def get_app_state(key: str) -> str | None:
    """读取应用状态键值。"""
    row = get_db().query_one("SELECT value FROM app_state WHERE key=?", (key,))
    return row["value"] if row else None


def set_app_state(key: str, value: str) -> None:
    """写入应用状态键值（app_state 为 key 主键表，不走通用 insert）。"""
    d = get_db()
    existing = d.query_one("SELECT key FROM app_state WHERE key=?", (key,))
    if existing:
        d.execute("UPDATE app_state SET value=?, update_time=? WHERE key=?",
                  (value, now_str(), key))
    else:
        d.execute("INSERT INTO app_state (key, value, update_time) VALUES (?, ?, ?)",
                  (key, value, now_str()))
