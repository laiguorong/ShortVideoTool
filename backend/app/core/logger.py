# -*- coding: utf-8 -*-
"""loguru 初始化（拆为两段以适配异步启动流程）。

设计：
- init_logger_console() — 骨架阶段，仅 stderr sink（不依赖 data_dir）
- init_logger_file()    — check_data_dir 完成后挂文件 sink 到 data/log/app_YYYYMMDD.log

拆分原因：loguru 文件 sink 路径依赖 get_data_dir()，而 data_dir 是步骤 2 才初始化的。
骨架阶段（on_startup 自身 + 任何阶段 1 之前的诊断日志）只能走 stderr。
"""

import os
import sys as _sys
import threading

from loguru import logger

# 幂等保护：init_logger_file 重复调用不重复挂 sink
# FastAPI sync 路由跑在线程池，用 threading.Event 保证原子性（多线程并发安全）
_logger_file_added = threading.Event()


def init_logger_console() -> None:
    """骨架阶段：开 VT + stderr sink + 接管 stdlib logging。

    不挂文件 sink（依赖 data_dir）。
    重复调用幂等（loguru.add 自动处理同 sink 不重复添加）。
    """
    # 任务 #73：Windows 原生 cmd 默认不解析 ANSI。空命令 os.system("") 会触发 cmd 启动流程，
    # 副作用开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING，让 ANSI 颜色码渲染。
    if _sys.platform == "win32":
        try:
            os.system("")
        except Exception:  # noqa: BLE001
            pass

    import logging

    class _InterceptHandler(logging.Handler):
        """标准 logging → loguru 桥接（uvicorn access/error / apscheduler）。"""

        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # 任务 #69：过滤 Windows ProactorEventLoop 已知噪声（客户端断开 → ConnectionResetError）
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
            try:
                logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
            except BaseException:  # noqa: BLE001 退出期日志必须绝对静默
                pass

    # 关停期 stderr pipe 已被回收，写会触发 Proactor 异常。safe sink 兜底。
    def _safe_stderr_sink(message) -> None:
        try:
            if _sys.stderr is None or getattr(_sys.stderr, "closed", True):
                return
            _sys.stderr.write(str(message))
            _sys.stderr.flush()
        except BaseException:  # noqa: BLE001
            pass

    logger.remove()
    # 自定义函数 sink 不会自动应用 colorize，需显式 colorize=True
    logger.add(_safe_stderr_sink, level="DEBUG", colorize=True, format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSSZZ}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ))
    # catch=True：loguru 内部捕获 sink 抛出的异常，避免退出期触发级联报错
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "apscheduler"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False
    # #420：去掉后端接口请求日志
    logging.getLogger("uvicorn.access").handlers = [_InterceptHandler()]
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# 存文件 sink handler id（init_logger_file 重入时先 remove 旧的，确保幂等）
_file_sink_id: int | None = None


def init_logger_file() -> None:
    """check_data_dir 完成后调：挂 data/log/app_YYYYMMDD.log。

    依赖 get_data_dir()，调用前必须 init_data_dir 完成。
    幂等：重复调用不重复挂 sink（threading.Event 守卫；如已挂则先 remove 旧的重建）。
    """
    global _file_sink_id
    if _logger_file_added.is_set():
        return
    _logger_file_added.set()

    from app.services.setting_service import get_data_dir

    log_dir = get_data_dir() / "log"
    log_dir.mkdir(parents=True, exist_ok=True)  # 兜底：loguru 自身不建目录
    log_path = log_dir / "app_{time:YYYYMMDD}.log"
    _file_sink_id = logger.add(
        log_path,
        rotation="00:00",
        retention="30 days",
        level="DEBUG",
        encoding="utf-8",
        catch=True,
    )
