# -*- coding: utf-8 -*-
"""启动检查状态机（与前端启动页配合）。

设计：
- on_startup 仅做控制台 logger；7 步业务初始化拆为 check 函数注册到本模块
- 前端按 1→7 串行调 POST /api/startup/check/{key}
- check 已 ok 直接返缓存（禁止重复调用）；pending/running/failed 重跑
- check 函数内部必须 try/except 返 CheckResult，不得向外抛

启动检查依赖链：
  data_dir → database + settings → scheduler（cleanup 都放 scheduler 末尾）
  ffmpeg / playwright 与 data_dir/database 解耦，独立步骤
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from threading import Lock
from typing import Callable


@dataclass
class CheckResult:
    """单步检查结果。前端直接 asdict 序列化。"""

    key: str
    status: str = "pending"          # pending | running | ok | failed
    detail: str = ""                 # 人类可读（成功简述 / 失败原因）
    data: dict = field(default_factory=dict)   # 步骤相关数据（如 data_dir 路径、版本号）
    ts: str = ""                     # ISO 时间戳（前端可选展示）

    def to_dict(self) -> dict:
        return asdict(self)


# 全局状态：key → 上一次结果
_state: dict[str, CheckResult] = {}

# 全局注册表：key → check 函数（返回 CheckResult）
_runners: dict[str, Callable[[], CheckResult]] = {}

# 并发锁：check/race 期间保护状态切换
_lock = Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def register(key: str, runner: Callable[[], CheckResult]) -> None:
    """注册 check 函数（on_startup 阶段调用，不执行）。

    重复注册同一 key 会被覆盖（方便开发期热更）。
    """
    with _lock:
        _runners[key] = runner
        _state.setdefault(key, CheckResult(key=key, status="pending"))


def get(key: str) -> CheckResult:
    """获取某步当前状态（无记录返 pending）。"""
    return _state.get(key) or CheckResult(key=key, status="pending")


def get_all() -> dict[str, CheckResult]:
    """获取所有步骤状态（前端 GET /state 用）。"""
    with _lock:
        return dict(_state)


def check(key: str) -> CheckResult:
    """执行某步 check。

    - 已 ok：直接返缓存（禁止重复调用，避免重复跑 ffmpeg 实测等）
    - running：另一线程正在跑，返当前 running 状态（不并发触发 runner）
    - pending / failed：调用 runner 重跑
    - 未注册：返 failed
    """
    with _lock:
        runner = _runners.get(key)
        if runner is None:
            result = CheckResult(key=key, status="failed", detail=f"未注册的检查步骤: {key}", ts=_now_iso())
            _state[key] = result
            return result
        current = _state.get(key)
        if current and current.status == "ok":
            return current
        if current and current.status == "running":
            # 别的线程/请求正在跑这个 check，不要并发触发 runner
            return current
        _state[key] = CheckResult(key=key, status="running", ts=_now_iso())

    try:
        from loguru import logger
        logger.info(f"[启动检查] {key} 开始")
        result = runner()
        # runner 必须返 CheckResult；若 runner 内部忘了 set status，按异常处理
        if not isinstance(result, CheckResult):
            result = CheckResult(
                key=key, status="failed",
                detail=f"runner 返回类型错误: {type(result).__name__}",
                ts=_now_iso(),
            )
        elif not result.ts:
            result.ts = _now_iso()
        with _lock:
            _state[key] = result
        # 全部 6 步统一在此打日志，runner 内部不必各自 log
        if result.status == "ok":
            logger.info(f"[启动检查] {key} 完成: {result.detail}")
        else:
            logger.warning(f"[启动检查] {key} 失败: {result.detail}")
        return result
    except Exception as exc:  # noqa: BLE001 兜底：runner 自身未接住的异常
        result = CheckResult(
            key=key, status="failed",
            detail=f"{type(exc).__name__}: {exc}",
            ts=_now_iso(),
        )
        with _lock:
            _state[key] = result
        # logger 自身异常（stderr 已关 / sink 抛错）也不能再抛 — 与关停期 loguru 行为一致
        try:
            from loguru import logger
            logger.warning(f"[启动检查] {key} 异常: {result.detail}")
        except BaseException:  # noqa: BLE001 退出期日志必须绝对静默
            pass
        return result


def reset(keys: list[str] | None = None) -> None:
    """清缓存（重新跑某步前调；keys=None 清全部）。

    - ok 的步骤会被重置为 pending
    - running 中的步骤不动（避免并发竞争）
    - pending 不变（已是 pending，没必要再清一次）
    """
    with _lock:
        targets = keys if keys else list(_state.keys())
        for k in targets:
            cur = _state.get(k)
            # running 不动；pending 也跳过（无意义）；只清 ok / failed
            if cur and cur.status not in ("running", "pending"):
                _state[k] = CheckResult(key=k, status="pending")
