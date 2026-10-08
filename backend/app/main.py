# -*- coding: utf-8 -*-
"""应用入口：FastAPI 实例、启动初始化链、退出清理（参考 VideoMatrix 模式）。

启动顺序（异步化重构）：
- on_startup 仅做控制台 logger 初始化 + 注册 7 步 check 函数
- 7 步业务初始化（data_dir → database → settings → ffmpeg → playwright → scheduler）
  由前端启动页按 1→7 串行触发 POST /api/startup/check/{key}

为什么这样设计：
- 失败可定位：用户能实时看到每步状态
- 不阻断：单步失败时 UI 展示原因，不影响已成功的子系统
- 闭环消除：startup 不调业务步骤，避免步骤 2 之前就调了步骤 2 的逻辑
"""

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

# [乱码根因修复] PyInstaller bootloader（Windows）会强制把 sys.stdout/stderr 编码重置为 GetACP() = gbk/cp936，
# 即使 env PYTHONUTF8=1 + PYTHONIOENCODING=utf-8 都传到了 utf8_mode 仍是 0。
# 必须在写任何日志前 reconfigure，否则 logger sink 写出字节按 cp936 编码 → Node 端 UTF-8 解码乱码。
if getattr(sys, "frozen", False):
    try:
        if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        # stdin 同处理：未来若加交互 CLI（如 input()）不会被 cp936 阻塞
        if sys.stdin and hasattr(sys.stdin, 'reconfigure'):
            sys.stdin.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001 reconfigure 失败不阻塞启动
        pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core import task_scheduler
from app.core import startup_checks
from app.core.logger import init_logger_console

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

app = FastAPI(title="短视频工具 后端服务", version="1.0.2")

# CORS 全开（本地 Electron 渲染层跨端口访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """启动初始化链（精简版）。

    仅做：
    - 控制台 logger 初始化（init_logger_console，不依赖 data_dir）

    6 步业务 check 函数注册在 startup_checks 模块 import 时完成（不再在 on_startup 阶段
    注册，避免 health 已通 + startup router 还没注册导致的 404 race）。
    6 步业务初始化（data_dir → database → settings → ffmpeg → playwright → scheduler）
    由前端启动页按顺序串行触发 POST /api/startup/check/{key}。
    """
    from loguru import logger
    init_logger_console()
    logger.info("[启动] on_startup 完成，STEPS 已在模块加载时注册（{} 步），等待前端触发", len(startup_checks.STEPS))


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


def main() -> None:
    """直接启动入口（仅 `python -m app.main` 用；dev / 生产都不会进 main()）。

    dev 模式（uvicorn import 模式）/ 生产模式（PyInstaller 打包 run_backend.py）
    都不会进 main()。数据目录路径走 spawn env SHORTVIDEO_DATA_DIR 透传，
    后端 startup_checks.check_data_dir 自己读 env 解析。
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
