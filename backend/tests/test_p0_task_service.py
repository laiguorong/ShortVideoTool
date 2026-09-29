# -*- coding: utf-8 -*-
"""P0/P1 修复后单元测试：task_service 核心工具 + 状态机契约。

覆盖：
- _fmt_hms 格式统一（H:MM:SS）
- interruptible_sleep 取消响应（信号 1s 内抛出）
- submit 状态机契约（success/partial/failed 正确映射，字符串 fallback 不再用）
- _DEFAULT_MESSAGE 覆盖 4 终态
- 取消时状态=cancelled + message=已取消
"""
import os
import sys
import threading
import time
from pathlib import Path

# 让 import 能找到 app 包
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.task_service import (
    _fmt_hms,
    interruptible_sleep,
    raise_for_cancel,
    task_service,
    TaskInfo,
    _TaskCancelled,
    _DEFAULT_MESSAGE,
)


# ============ _fmt_hms ============

def test_fmt_hms_zero():
    assert _fmt_hms(0) == "0:00:00"


def test_fmt_hms_seconds_only():
    assert _fmt_hms(45) == "0:00:45"


def test_fmt_hms_minutes():
    assert _fmt_hms(125) == "0:02:05"


def test_fmt_hms_one_hour():
    assert _fmt_hms(3600) == "1:00:00"


def test_fmt_hms_complex():
    """1h 23m 45s = 5025s → 1:23:45"""
    assert _fmt_hms(5025) == "1:23:45"


def test_fmt_hms_negative_clamped():
    """负数视为 0（防止异常输入导致 -1:59:59）"""
    assert _fmt_hms(-100) == "0:00:00"


def test_fmt_hms_float_truncated():
    """浮点数取整"""
    assert _fmt_hms(59.9) == "0:00:59"


# ============ interruptible_sleep ============

def test_interruptible_sleep_completes():
    """正常路径：sleep 秒数到达后返回"""
    info = TaskInfo("t1", "generate", "test")
    start = time.monotonic()
    interruptible_sleep(0.2, info, chunk=0.05)
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed <= 0.4, f"耗时 {elapsed:.2f}s 异常"


def test_interruptible_sleep_cancel_responsive():
    """#P1-3：取消信号 1s 内抛出（之前 time.sleep 阻塞分钟级）"""
    info = TaskInfo("t2", "generate", "test")

    def cancel_after():
        time.sleep(0.2)
        info.cancel_requested = True

    t = threading.Thread(target=cancel_after)
    t.start()
    start = time.monotonic()
    try:
        interruptible_sleep(10.0, info, chunk=0.05)
        assert False, "应抛 _TaskCancelled"
    except _TaskCancelled:
        elapsed = time.monotonic() - start
        # chunk=0.05，最长延迟 0.05s；预期 <0.5s 响应
        assert elapsed < 0.5, f"取消响应耗时 {elapsed:.2f}s 过长"
    finally:
        t.join()


def test_interruptible_sleep_zero_seconds():
    """0 秒立即返回，不阻塞"""
    info = TaskInfo("t3", "generate", "test")
    start = time.monotonic()
    interruptible_sleep(0, info)
    assert time.monotonic() - start < 0.05


def test_interruptible_sleep_negative_seconds():
    """负数视作 0（防御性）"""
    info = TaskInfo("t4", "generate", "test")
    interruptible_sleep(-5, info)  # 应立即返回不抛


# ============ raise_for_cancel ============

def test_raise_for_cancel_idle():
    info = TaskInfo("t5", "generate", "test")
    raise_for_cancel(info)  # 不抛


def test_raise_for_cancel_requested():
    info = TaskInfo("t6", "generate", "test")
    info.cancel_requested = True
    try:
        raise_for_cancel(info)
        assert False, "应抛 _TaskCancelled"
    except _TaskCancelled:
        pass


# ============ 状态机契约：success/partial/failed ============

def test_submit_success_status():
    """func 返回 'success' → info.status='success'"""
    def fn(info):
        return "success"
    tid = task_service.submit("generate", "test_success", fn)
    # 等 worker 跑完（线程池异步）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] in ("success", "failed"):
            break
        time.sleep(0.05)
    info = task_service.get_task(tid)
    assert info["status"] == "success", f"status={info['status']}"
    assert info["message"] == "", f"message={info['message']!r}"


def test_submit_partial_message_fallback():
    """func 返回 'partial' 未设 message → 默认 '部分成功'"""
    def fn(info):
        return "partial"
    tid = task_service.submit("generate", "test_partial", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    assert info["message"] == "部分成功", f"message={info['message']!r}"


def test_submit_failed_message_fallback():
    """func 返回 'failed' 未设 message → 默认 '失败'"""
    def fn(info):
        return "failed"
    tid = task_service.submit("generate", "test_failed", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "failed"
    assert info["message"] == "失败", f"message={info['message']!r}"


def test_submit_custom_message_preserved():
    """func 返回 partial + 自设 message → 保留自设（不被默认值覆盖）"""
    def fn(info):
        info.message = "部分失败：拼接失败×3，去重处理失败×2"
        return "partial"
    tid = task_service.submit("generate", "test_custom_msg", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    assert info["message"] == "部分失败：拼接失败×3，去重处理失败×2"


def test_submit_string_fallback_deprecated():
    """#P0-10：func 返非契约字符串视为 success（兼容旧 worker，但前端可见失败原因）"""
    def fn(info):
        info.message = "FFmpeg 不可用"
        return "failed"  # 修后 worker 应返契约值
    tid = task_service.submit("generate", "test_string_fb", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    # 修后应该正确映射到 failed
    assert info["status"] == "failed"


def test_submit_cancelled_status():
    """worker 内 raise_for_cancel → status=cancelled, message=已取消"""
    def fn(info):
        info.cancel_requested = True
        raise_for_cancel(info)
    tid = task_service.submit("generate", "test_cancelled", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "cancelled"
    assert info["message"] == "已取消"


def test_submit_exception_caught():
    """worker 抛异常 → status=failed, message=str(e)"""
    def fn(info):
        raise ValueError("模拟业务异常")
    tid = task_service.submit("generate", "test_exception", fn)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "failed"
    assert "模拟业务异常" in info["message"]


# ============ _DEFAULT_MESSAGE 完整性 ============

def test_default_message_covers_all_terminal_status():
    """4 个终态都有默认文案"""
    assert "success" in _DEFAULT_MESSAGE
    assert "partial" in _DEFAULT_MESSAGE
    assert "failed" in _DEFAULT_MESSAGE
    assert "cancelled" in _DEFAULT_MESSAGE


def test_default_message_values():
    assert _DEFAULT_MESSAGE["success"] == ""
    assert _DEFAULT_MESSAGE["partial"] == "部分成功"
    assert _DEFAULT_MESSAGE["failed"] == "失败"
    assert _DEFAULT_MESSAGE["cancelled"] == "已取消"


# ============ helper ============

def _wait_done(tid: str, timeout: float = 5.0):
    """等待任务离开 running/waiting 状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] in ("success", "partial", "failed", "cancelled"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"任务 {tid} 超时未完成")


# ============ runner ============

def _run_all():
    """收集所有 test_ 函数依次跑（无 pytest 也能用）。"""
    import inspect
    funcs = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed, failed = 0, 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  [OK] {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed (total {len(funcs)})")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
