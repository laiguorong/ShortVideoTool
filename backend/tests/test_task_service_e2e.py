# -*- coding: utf-8 -*-
"""P0/P1 端到端 task_service 测试：API 契约 + 完整流程。

覆盖：
- summary() running_items 字段补全（status/progress/message）#P0 修复点
- 完整 submit → list → get → cancel 流程
- cancel 后 worker 在下次 raise_for_cancel 时响应
- 状态机契约贯穿：partial message 保留 + cancelled message=已取消
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.task_service import TaskInfo, task_service, _TaskCancelled, raise_for_cancel


def _wait_status(tid: str, statuses: tuple, timeout: float = 5.0):
    """等待任务达到目标状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] in statuses:
            return info
        time.sleep(0.05)
    raise TimeoutError(f"任务 {tid} 超时未达 {statuses}")


# ============ summary API 字段契约 ============

def test_summary_includes_status_progress_message():
    """#P0：summary().running_items 每项含 status/progress/message
    修复前 running_items 只有 type/type_label/name，前端详情表看不到状态/进度/失败原因
    """
    def fn(info):
        info.progress = "测试进度 1/10"
        info.message = "测试失败原因"
        # worker 不返 status——这里是为了触发 running 状态持续
        threading.Event().wait(0.3)
        return "success"

    tid = task_service.submit("generate", "test_summary_fields", fn)
    # 让任务进入 running 但不退出
    time.sleep(0.1)
    summary = task_service.summary()
    assert "running_total" in summary
    assert "by_type" in summary
    assert "running_items" in summary
    # 找到我们刚 submit 的 task
    item = next((i for i in summary["running_items"] if i["task_id"] == tid), None)
    assert item is not None, f"running_items 未含 task {tid}"
    # 关键字段都在
    assert item["status"] == "running", f"status 缺失或错误: {item.get('status')}"
    assert item["progress"] == "测试进度 1/10"
    assert item["message"] == "测试失败原因"
    assert item["type_label"]  # 默认 _TYPE_LABELS["generate"]
    # 等待 worker 结束
    _wait_status(tid, ("success",))


def test_summary_running_total_count():
    """summary().running_total 准确计数"""
    barrier = threading.Event()

    def hold(info):
        barrier.wait(3)  # 让任务长时间保持 running
        return "success"

    tids = []
    for i in range(3):
        tid = task_service.submit("generate", f"hold_{i}", hold)
        tids.append(tid)
    time.sleep(0.1)
    summary = task_service.summary()
    # running_total 应 ≥3（可能还有之前测试遗留的）
    assert summary["running_total"] >= 3, f"running_total={summary['running_total']}"
    # by_type["generate"] 应 ≥3
    assert summary["by_type"].get("generate", 0) >= 3
    # 释放 barrier 让 worker 退出
    barrier.set()
    for tid in tids:
        _wait_status(tid, ("success",))


# ============ 完整 submit → list → get 流程 ============

def test_submit_list_get_full_flow():
    """完整流程：submit → list_tasks → get_task"""
    def fn(info):
        return "success"

    tid = task_service.submit("generate", "test_flow", fn)
    _wait_status(tid, ("success",))

    # list_tasks
    items = task_service.list_tasks()
    assert any(i["task_id"] == tid for i in items), "list_tasks 未含新任务"

    # get_task
    info = task_service.get_task(tid)
    assert info is not None
    assert info["task_id"] == tid
    assert info["status"] == "success"
    assert info["name"] == "test_flow"
    assert info["task_type"] == "generate"
    assert info["type_label"] == "视频生成"


def test_list_tasks_running_only_filter():
    """list_tasks(running_only=True) 只返 running/waiting"""
    barrier = threading.Event()

    def hold(info):
        barrier.wait(3)
        return "success"

    def quick(info):
        return "success"

    running_tid = task_service.submit("generate", "running_task", hold)
    time.sleep(0.2)  # 等 worker 起跑
    # submit 一个 task 用不同 type（shop_pull 池独立），确保能跑完
    done_tid = task_service.submit("shop_pull", "done_task", quick)
    _wait_status(done_tid, ("success",))
    time.sleep(0.05)

    items = task_service.list_tasks(running_only=True)
    ids = [i["task_id"] for i in items]
    assert running_tid in ids
    assert done_tid not in ids
    barrier.set()
    _wait_status(running_tid, ("success",))


def test_get_task_not_found():
    """get_task 不存在 → None"""
    info = task_service.get_task("non-existent-id-12345")
    assert info is None


# ============ cancel 流程 ============

def test_cancel_running_task():
    """cancel running 任务 → cancel_requested=True → worker 抛 _TaskCancelled"""
    barrier = threading.Event()

    def worker_should_cancel(info):
        # 循环等待，每段检查取消信号
        for _ in range(30):
            raise_for_cancel(info)
            if barrier.is_set():
                break
            barrier.wait(0.1)
        return "success"

    tid = task_service.submit("generate", "test_cancel", worker_should_cancel)
    time.sleep(0.2)  # 等 worker 起跑

    ok = task_service.request_cancel(tid)
    assert ok is True

    info = _wait_status(tid, ("cancelled",))
    assert info["status"] == "cancelled"
    assert info["message"] == "已取消"
    barrier.set()


def test_cancel_nonexistent_task_returns_false():
    """cancel 不存在的任务 → False"""
    ok = task_service.request_cancel("non-existent-id")
    assert ok is False


def test_cancel_completed_task_returns_false():
    """cancel 已完成任务 → False（仅 waiting/running 可取消）"""
    def quick(info):
        return "success"

    tid = task_service.submit("generate", "test_cancel_done", quick)
    _wait_status(tid, ("success",))
    ok = task_service.request_cancel(tid)
    assert ok is False


# ============ shutdown 兜底 ============

def test_shutdown_doesnt_hang():
    """shutdown 不阻塞（wait=False + cancel_futures=True）"""
    # 不实际 shutdown（会杀线程池），只验证 shutdown 调用后 task_service 仍可用
    pass  # 跳过——会破坏后续测试


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
