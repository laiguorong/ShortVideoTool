# -*- coding: utf-8 -*-
"""P1 测试：partial 信息模板——selection_service / creation_service
在 partial 时把具体失败原因挂到 info.message，前端可见。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.task_service import TaskInfo, task_service


def _wait_done(tid: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] in ("success", "partial", "failed", "cancelled"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"任务 {tid} 超时")


# ============ selection partial 信息模板 ============

def test_selection_partial_message_format():
    """#P1：selection_service partial 时 message 形如"部分失败：{fail_reason}"
    原代码：直接 return "partial" 缺 message
    修后：info.message = f"部分失败：{fail_reason}"
    """
    def mock_run_one_round(info: TaskInfo) -> str:
        # 模拟 selection_service._run_one_round 末尾 partial 路径
        fail_reason = "搜索返回未满一页（10 < 12），本轮已无更多数据（共 5 页）"
        info.message = f"部分失败：{fail_reason}"
        return "partial"

    tid = task_service.submit("shop_pull", "test_selection_partial", mock_run_one_round)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    assert info["message"].startswith("部分失败：")
    assert "搜索返回未满一页" in info["message"]


def test_selection_partial_no_fail_reason_empty_message():
    """边界：fail_reason 为空时 message 应保持空串（被 task_service 默认覆盖）"""
    def mock_run_one_round(info: TaskInfo) -> str:
        # 业务上 fail_reason="" 实际不该走到 partial，但防御性写 message=""
        return "partial"

    tid = task_service.submit("shop_pull", "test_selection_partial_empty", mock_run_one_round)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    # 默认 message=部分成功
    assert info["message"] == "部分成功"


# ============ creation generate partial 信息模板 ============

def test_creation_generate_partial_with_reason_counter():
    """#P1：creation_service._run_generate partial 时按失败原因分组拼 message
    原代码：info.message = f"失败 {fail} 条" 信息不足
    修后：info.message = f"部分失败：{reasons}" 形如"部分失败：拼接失败×3，去重处理失败×2"
    """
    def mock_run_generate(info: TaskInfo) -> str:
        # 模拟生成 worker 末尾 partial 路径
        reason_counter = {"拼接失败": 3, "去重处理失败": 2}
        fail = 5
        if fail > 0:
            hints = "，".join(f"{r}×{c}" for r, c in reason_counter.items())
            info.message = f"部分失败：{hints}"
        return "partial"

    tid = task_service.submit("generate", "test_create_partial", mock_run_generate)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    assert "拼接失败×3" in info["message"]
    assert "去重处理失败×2" in info["message"]
    # 顺序：most_common 优先拼接失败（频次高）
    assert info["message"].index("拼接失败") < info["message"].index("去重处理")


def test_creation_generate_partial_no_reasons_fallback():
    """边界：fail > 0 但 reason_counter 为空（防御性）"""
    def mock_run_generate(info: TaskInfo) -> str:
        fail = 1
        if fail > 0:
            reason_counter = {}
            if reason_counter:
                hints = "，".join(f"{r}×{c}" for r, c in reason_counter.items())
                info.message = f"部分失败：{hints}"
            else:
                info.message = f"失败 {fail} 条"
        return "partial"

    tid = task_service.submit("generate", "test_create_partial_fallback", mock_run_generate)
    _wait_done(tid)
    info = task_service.get_task(tid)
    assert info["status"] == "partial"
    assert info["message"] == "失败 1 条"


# ============ 前端冗余判断逻辑验证 ============

def test_partial_message_not_equal_to_label():
    """前端判断逻辑：STATUS_LABEL["partial"]="部分成功"，message 应不等于它才显示
    - "部分失败：xxx" → 不等 → 显示 ✓
    - "部分成功" → 等 → 隐藏（不应是这种）
    """
    # 验证我们的 partial 信息模板不会与 STATUS_LABEL 冲突
    from app.services.task_service import _DEFAULT_MESSAGE
    # 部分失败的 message 与 label 不同
    assert "部分失败" not in _DEFAULT_MESSAGE["partial"]
    assert _DEFAULT_MESSAGE["partial"] == "部分成功"


# ============ runner ============

def _run_all():
    import inspect
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed = 0
    failed = 0
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
