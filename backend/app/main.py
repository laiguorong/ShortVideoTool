# -*- coding: utf-8 -*-
"""应用入口：FastAPI 实例、启动初始化链、退出清理（参考 VideoMatrix 模式）。

启动顺序（需求文档 F-08.6 初始化闭环）：
1. data 目录初始化（8 子目录）；
2. 日志初始化（loguru → data/log/）；
3. 加密模块初始化（程序目录 config/.key，不随数据目录漂移）；
4. 数据库初始化 + 建表迁移；
5. 客户端模式加载（mock/real）；
6. 调度器启动 + 定时任务恢复（enabled 拉取任务 / 登录态检测）；
7. 通知超期清理。
"""

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

# 注：不要切 WindowsSelectorEventLoopPolicy。
# Playwright 在 Windows 上启动 Chromium 走 asyncio.create_subprocess_exec，
# SelectorEventLoop 不支持子进程 → 抛 NotImplementedError。

# 但 ProactorEventLoop 关停期 _call_connection_lost 在 pipe 已被客户端 RST 时
# 会触发 ConnectionResetError → asyncio.default_exception_handler 直接写 stderr
# （绕开 loguru sink），pipe 已关 → 再抛 OSError/ValueError，traceback 雪崩。
# monkey-patch ProactorEventLoop._call_connection_lost：ConnectionResetError 直接吞。
if sys.platform == "win32":
    try:
        import asyncio.windows_events as _we

        _orig_call_connection_lost = _we.ProactorEventLoop._call_connection_lost

        def _patched_call_connection_lost(self, protocol, exc=None):
            if isinstance(exc, ConnectionResetError):
                return
            return _orig_call_connection_lost(self, protocol, exc)

        _we.ProactorEventLoop._call_connection_lost = _patched_call_connection_lost
    except Exception:  # noqa: BLE001 patch 失败不阻塞启动
        pass

# 打包环境下切换工作目录到 exe 同级（保证找到 data/ 与 ffmpeg）
if getattr(sys, "frozen", False):
    import os
    os.chdir(Path(sys.executable).parent)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core import task_scheduler
from app.core import notifier
from app.db import init_db
from app.db.migrations import migrate
from app.services import setting_service
from app.services.setting_service import get_app_dir, get_data_dir, init_app_dirs, init_data_dir

# #112 / #121：Windows 下 `python -m uvicorn` 收到 SIGINT 后 asyncio loop 经常卡死无法 graceful shutdown
# （uvicorn 已知 bug）。通过 patch uvicorn.Server.handle_exit：第一次收到信号 → graceful；
# 1.5 秒后还没退（用单调时钟核对，避免系统时间回退误判）→ 强制 force_exit=True。
# 之前要求"1 秒内二次 Ctrl+C 才强退"对用户不友好（用户多半只会按一次）；
# 改为单次 Ctrl+C 触发后 1.5s 自动强退，再加 on_shutdown 末尾的 2s watchdog 兜底。
def _patch_uvicorn_force_exit() -> None:
    try:
        import uvicorn
        _orig_handle_exit = uvicorn.Server.handle_exit

        def _patched_handle_exit(self, sig, frame=None):
            # 第一次：让 uvicorn 走 graceful shutdown
            self.should_exit = True
            # 1.5 秒后如果还没退，直接 force_exit=True
            import threading as _t
            import time as _time
            import os as _os

            def _kick_force_exit():
                start = _time.monotonic()
                _time.sleep(1.5)
                try:
                    # 已退出标志（uvicorn 退出后会设 _force_exit_done；这里不强依赖）
                    self.force_exit = True
                    from loguru import logger
                    logger.warning(f"[ForceExit] 收到信号 {sig} 后 1.5s 进程未退，强制 force_exit")
                except Exception:
                    pass
                # 额外兜底：再 0.5s 后如果进程还在，硬退（os._exit 不抛异常，最暴力）
                _time.sleep(0.5)
                try:
                    _os._exit(0)
                except Exception:
                    pass

            _t.Thread(target=_kick_force_exit, daemon=True, name="force-exit-kicker").start()

        uvicorn.Server.handle_exit = _patched_handle_exit
    except Exception:
        pass

_patch_uvicorn_force_exit()

app = FastAPI(title="短视频工具 后端服务", version="1.0.1")

# CORS 全开（本地 Electron 渲染层跨端口访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """启动初始化链。"""
    from loguru import logger
    # 1. 应用目录（不可变；config/ 与 models/ 固定在程序目录；#361）
    init_app_dirs()
    # 2. 本地数据目录（可改路径；首次自动选最大盘根下 ShortVideoToolData）
    init_data_dir(_args.data_dir and Path(_args.data_dir) or None)
    # 3. 日志
    _init_logger()
    # 4. 加密（密钥放应用目录 config/，不随数据目录漂移）
    from app.core import crypto
    from app.services.setting_service import get_config_dir
    crypto.init_crypto(get_config_dir())
    # 5. 数据库 + 迁移
    init_db(get_data_dir() / "db" / "short_video_tools.db")
    version = migrate(_get_db_instance())
    # 4.1 历史空串 category_id → 未分类虚拟 ID（一次性数据迁移）
    from app.services.material_service import migrate_empty_category_to_uncategorized
    migrated = migrate_empty_category_to_uncategorized(_get_db_instance())
    if migrated:
        logger.info("[启动迁移] category_id='' → '-'：{} 条", migrated)
    # 4.2 #363：项目片段路径从 create/clip/* + cache/clip/* 搬到 project/<pid>/...
    from app.services.creation_service import migrate_clip_paths_to_project
    clip_mig = migrate_clip_paths_to_project()
    if clip_mig["moved_files"] or clip_mig["updated_rows"]:
        logger.info("[启动迁移] 片段路径 project/ 化：搬移 {} 个文件，更新 {} 行",
                    clip_mig["moved_files"], clip_mig["updated_rows"])
    # 4.3 #364：素材按 id 子目录聚合（视频/封面/头像）
    from app.services.material_service import migrate_material_paths_to_id_subdir
    mat_mig = migrate_material_paths_to_id_subdir()
    if mat_mig["moved_files"] or mat_mig["updated_rows"]:
        logger.info("[启动迁移] 素材按 id 子目录化：搬移 {} 个文件，更新 {} 行",
                    mat_mig["moved_files"], mat_mig["updated_rows"])
    # 5. 加载配置 + 调度器 + 定时任务恢复
    settings = setting_service.load_settings()
    task_scheduler.start_scheduler()
    _register_scheduled_jobs()
    # 6.1 任务 #381：启动期检查 ffmpeg + libx264 可用，不可用直接中断
    from app.core.ffmpeg import check_ffmpeg_env
    check_ffmpeg_env()  # 不可用抛 RuntimeError，中断 FastAPI 启动
    # 7. 通知清理 + 启动残留 temp 清理
    notifier.cleanup_expired(settings.get("log_retention_days", 30))
    setting_service.cleanup_startup_temp(settings.get("temp_retention_days", 7))
    logger.info("[启动完成] 应用目录：{}；数据目录：{}；数据库版本：{}", get_app_dir(), get_data_dir(), version)
    # #bug 恢复：dispatch 双层抢占 fix 后，重启时把卡在 waiting 的 running 任务全部派发
    # （之前可能因 bug 残留 publishing→回滚→waiting，需要主动 dispatch 唤醒 worker）
    try:
        from app.services.publish_service import dispatch_all_waiting
        d = _get_db_instance()
        for r in d.query_all("SELECT id FROM publish_task WHERE status='running' AND deleted=0"):
            n = dispatch_all_waiting(r["id"])
            if n:
                logger.info("[启动恢复] task={} 派发 {} 条 waiting", r["id"][:8], n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[启动恢复] dispatch 异常: {}", exc)


def _get_db_instance():
    """获取数据库实例（startup 内使用，此时已 init）。"""
    from app.db import get_db
    return get_db()


def _register_scheduled_jobs() -> None:
    """恢复定时任务：enabled 拉取任务（M3 提供注册回调）+ 登录态检测。"""
    # 登录态检测（M1 已有）
    from app.services.account_service import register_account_check_job
    register_account_check_job()
    # 拉取任务恢复（M3 的 selection/material 服务就绪后接入；当前空实现占位）
    try:
        from app.services import selection_service, material_service  # noqa: F401 M3 提供
        task_scheduler.register_enabled_tasks(
            register_shop_pull=selection_service.register_shop_pull,
            register_video_pull=material_service.register_task_job,
        )
    except ImportError:
        pass  # M3 未生成时跳过


def _init_logger() -> None:
    """loguru 初始化：按天滚动落 data/log/，同时保留控制台输出。

    并接管标准 logging（uvicorn/apscheduler 等），使 HTTP 访问日志也落文件。
    """
    # 任务 #73：Windows 原生 cmd 默认不解析 ANSI 转义序列。空命令 os.system("")
    # 会触发 cmd 启动流程，副作用是开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING，
    # 之后 ANSI 颜色码才能渲染（在 Windows Terminal/PowerShell/新版 cmd 已默认开）。
    if sys.platform == "win32":
        try:
            os.system("")
        except Exception:  # noqa: BLE001
            pass
    from loguru import logger
    import logging
    import sys as _sys

    class _InterceptHandler(logging.Handler):
        """标准 logging → loguru 桥接（uvicorn access/error 一并接管）。"""

        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # 任务 #69：过滤 Windows ProactorEventLoop 已知噪声。客户端断开后服务端
            # socket.shutdown(SHUT_RDWR) 必抛 ConnectionResetError [WinError 10054]，
            # uvicorn 通过 logging.ERROR 报告，loguru 接力打 ERROR traceback 雪崩。
            # 触发位置：asyncio/proactor_events.py _call_connection_lost → 降 DEBUG。
            if record.exc_info:
                exc = record.exc_info[1]
                if isinstance(exc, ConnectionResetError):
                    tb = record.exc_info[2]
                    while tb is not None:
                        co = tb.tb_frame.f_code
                        if co.co_name == "_call_connection_lost" and co.co_filename.endswith("proactor_events.py"):
                            level = "DEBUG"
                            break
                        tb = tb.tb_next
            # 找到真实调用帧（跳过 logging 内部帧）
            frame, depth = None, 0
            if record.exc_info:
                frame = logging.currentframe()
            else:
                frame = logging.currentframe().f_back
            while frame and depth < 20:
                filename = frame.f_code.co_filename
                is_logging = filename == logging.__file__
                is_stdlib = filename.startswith(_sys.prefix) or filename.startswith("/usr/lib")
                if not (is_logging or is_stdlib):
                    break
                frame = frame.f_back
                depth += 1
            # 兜底：uvicorn 关闭时 stderr pipe 已被回收，向 stderr 写日志会触发
            # _ProactorBasePipeTransport._call_connection_lost 异常。仅记到文件
            # handler（pipe 关闭不影响文件），避免 ERROR 级日志雪崩。
            # 用 BaseException 而非 Exception：Windows 关停时可能冒 CancelledError
            # / _Overlapped... 取消（属 BaseException），否则会穿透到 asyncio loop 顶层。
            try:
                logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
            except BaseException:  # noqa: BLE001 退出期日志必须绝对静默
                pass

    # Windows 关停期 stderr pipe 已被回收，直接写会触发 Proactor 异常。
    # 用 safe sink 兜底：写失败时静默丢弃，避免 loguru 内部 ERROR 级递归再触发。
    def _safe_stderr_sink(message) -> None:
        try:
            # 同时判 stderr 是否已关闭（uvicorn 关停时常见）
            if _sys.stderr is None or getattr(_sys.stderr, "closed", True):
                return
            _sys.stderr.write(str(message))
            _sys.stderr.flush()
        except BaseException:  # noqa: BLE001 退出期日志必须绝对静默
            pass

    logger.remove()
    # 任务 #73：自定义函数 sink 不会自动应用 colorize，需显式 colorize=True
    # （loguru 已在 format 阶段注入 ANSI 转义序列，Windows 已通过 os.system("") 开 VT）
    logger.add(_safe_stderr_sink, level="DEBUG", colorize=True, format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ))
    # catch=True：loguru 内部捕获 sink 抛出的异常（写入 stderr 失败等），
    # 避免退出期触发 ProactorBasePipeTransport._call_connection_lost 级联报错。
    logger.add(
        get_data_dir() / "log" / "app_{time:YYYYMMDD}.log",
        rotation="00:00", retention="30 days", level="DEBUG", encoding="utf-8",
        catch=True,
    )
    # 标准日志根接管（uvicorn.error / apscheduler 等全量进入 loguru）
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "apscheduler"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False
    # #420：去掉后端接口请求日志（自定义中间件已删，uvicorn.access 也静默）
    logging.getLogger("uvicorn.access").handlers = [_InterceptHandler()]
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@app.on_event("shutdown")
def on_shutdown() -> None:
    """退出清理：停调度器、收尾任务池、关浏览器执行器。

    每步用 daemon 线程 + Event.wait(timeout) 包一层超时（#112 加强）：
    任何 cleanup 阻塞超过 1.5 秒就跳过，避免整个 on_shutdown 卡住。

    #121：uvicorn 收到 SIGINT 后 asyncio loop 经常卡死（Windows 已知 bug），
    即便 should_exit=True 也不退出。on_shutdown 末尾启动一个 watchdog 线程，
    2 秒后无论 uvicorn 是否退出，直接 os._exit(0) 兜底，
    避免 BackgroundScheduler 的 _event_loop daemon 线程继续触发（如 _timed_check_all）。
    """
    def _with_timeout(name: str, fn, timeout: float = 1.5):
        from loguru import logger
        done = threading.Event()

        def _runner():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 退出清理全兜底
                logger.exception(f"[Shutdown] {name} 异常: {e}")
            finally:
                done.set()

        t = threading.Thread(target=_runner, daemon=True, name=f"shutdown-{name}")
        t.start()
        if not done.wait(timeout=timeout):
            logger.warning(f"[Shutdown] {name} 超时（{timeout}s），跳过等待")

    _with_timeout("scheduler", task_scheduler.shutdown_scheduler)
    from app.services.task_service import task_service
    _with_timeout("task_service", task_service.shutdown)
    try:
        from app.core.douyin.browser import browser_actor
        _with_timeout("browser", browser_actor.shutdown)
    except Exception:  # noqa: BLE001 导入失败不影响退出
        pass

    # #121：兜底强退（不依赖 uvicorn 退出路径）。2 秒后无论 asyncio loop 是否退出，
    # 都直接 os._exit(0)。在此之前 scheduler 已被关，新 fire 不再发生；
    # daemon 线程会被强杀，避免继续触发定时任务（#112 用户实测 _timed_check_all 14 秒后还 fire）。
    import os as _os
    def _watchdog_exit():
        import time as _time
        _time.sleep(2.0)
        try:
            from loguru import logger
            logger.warning("[Shutdown] 2s watchdog 触发，os._exit(0) 兜底强退")
        except Exception:
            pass
        try:
            _os._exit(0)
        except Exception:
            pass
    threading.Thread(target=_watchdog_exit, daemon=True, name="shutdown-watchdog").start()


@app.get("/api/health", summary="健康检查")
def health():
    """存活探针（Electron 启动轮询用）。"""
    return {"ok": True, "service": "shortvideo-tool"}


app.include_router(api_router, prefix="/api")

# 命令行参数（必须在模块顶部解析，不能只放 main() 里）
# 原因：dev 模式 Electron spawn `python -m uvicorn app.main:app`，
# uvicorn 是 import 模式，`if __name__ == "__main__": main()` 不跑，
# _args 必须 import 时就读 env 透传的 SHORTVIDEO_DATA_DIR。
# argparse 不放 main()（line 366 旧版）就永远拿到 Namespace(data_dir=None)，
# init_data_dir(None) 走 resolve_default_data_dir 读 settings.json（H 盘），
# 用户在 SettingsPage 改路径后 SettingsPage 永远显示旧值（#data-dir-relaunch）。
# 守卫 SHORTVIDEO_TOOL_RUNTIME：仅在 Electron 主进程 spawn 时设的运行时环境变量下
# 才信任 env 透传，避免 pytest fixture import 时读到 CI shell 误设的 SHORTVIDEO_DATA_DIR
# 导致 data_dir 被污染。主进程 spawn 时 baseEnv 自动 export 此标记。
_env_data_dir = (
    os.environ.get("SHORTVIDEO_DATA_DIR")
    if os.environ.get("SHORTVIDEO_TOOL_RUNTIME")
    else None
)
_args = argparse.Namespace(data_dir=_env_data_dir)


def main() -> None:
    """直接启动入口（仅 `python -m app.main` 用；dev / 生产都不会进 main()）。

    dev 模式（uvicorn import 模式）/ 生产模式（PyInstaller 打包 run_backend.py）
    都不会进 main()——它们的 _args 由模块顶部 line 372-377 构造（含 SHORTVIDEO_TOOL_RUNTIME 守卫）。
    本函数只做 uvicorn 启动 + Windows 信号兜底。
    """
    import uvicorn
    # #112：timeout_graceful_shutdown=2 防止 Windows 上 asyncio loop graceful shutdown 卡死
    config = uvicorn.Config(
        app, host="127.0.0.1", port=8765, log_config=None,
        timeout_graceful_shutdown=2,  # 即使 force_exit 失效，2 秒后也会强退
    )
    server = uvicorn.Server(config)

    # 强制退出兜底：信号触发后置 should_exit=true 让 uvicorn 自杀
    def _force_exit(signum, frame):
        server.should_exit = True

    signal.signal(signal.SIGINT, _force_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _force_exit)

    server.run()


if __name__ == "__main__":
    main()
