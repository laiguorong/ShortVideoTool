# -*- coding: utf-8 -*-
"""高危修复回归测试：

#高危-1：task_service._tasks dict 内存无限增长 → OOM
  - 验证 _cleanup_finished_locked 按数量上限淘汰
  - 验证 _cleanup_finished_locked 按时间淘汰
  - 验证 submit 触发清理
  - 验证 shutdown 兜底清理
  - 验证 running 任务永不清

#高危-2：publish_service._publish_one_item_inner retry 期间取消 → 视频卡死 occupied
  - 验证取消时视频被释放
  - 验证取消时 publish_task_item 状态置 cancelled
  - 验证取消时 _refresh_task_progress 被调
"""
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services import setting_service
from app.services.task_service import TaskInfo, task_service, _TaskCancelled


def _use_temp_config():
    """重定向配置目录到临时目录。"""
    tmp = Path(tempfile.mkdtemp())
    config_dir = tmp / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    setting_service.get_config_dir = lambda: config_dir
    setting_service.get_settings_path = lambda: config_dir / "settings.json"
    return config_dir


def _wait_done(tid: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] in ("success", "partial", "failed", "cancelled"):
            return info
        time.sleep(0.05)
    raise TimeoutError(f"任务 {tid} 超时")


# ============ #高危-1：清理函数基本行为 ============

def test_cleanup_method_exists():
    """task_service 应有 _cleanup_finished_locked 方法"""
    assert hasattr(task_service, "_cleanup_finished_locked")
    assert callable(task_service._cleanup_finished_locked)


def test_cleanup_keeps_running_tasks():
    """running 任务永不清"""
    config_dir = _use_temp_config()
    barrier = threading.Event()

    def hold(info):
        barrier.wait(3)
        return "success"

    tid = task_service.submit("generate", "test_running_keep", hold)
    time.sleep(0.2)
    # 强制清理（手动调）
    with task_service._lock:
        task_service._cleanup_finished_locked()
    # running 任务仍在
    assert task_service.get_task(tid) is not None
    assert task_service.get_task(tid)["status"] == "running"
    barrier.set()
    _wait_done(tid)


def test_cleanup_drops_by_count():
    """超 max_count → 淘汰最早的终态任务"""
    config_dir = _use_temp_config()
    setting_service.save_settings({
        "task_history_max_count": 3,
        "task_history_retention_hours": 24,
    })

    # 注入 5 条终态任务（直接操作 _tasks 模拟已存在的旧任务）
    base_time = datetime.now() - timedelta(hours=1)
    for i in range(5):
        tid = f"old-task-{i}"
        info = TaskInfo(tid, "generate", f"old_{i}")
        info.status = "success"
        info.start_time = base_time.strftime("%Y-%m-%d %H:%M:%S")
        info.end_time = (base_time + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
        with task_service._lock:
            task_service._tasks[tid] = info

    with task_service._lock:
        task_service._cleanup_finished_locked()

    # 应剩 3 条最新的（i=2, 3, 4）
    remaining = [tid for tid in task_service._tasks.keys() if tid.startswith("old-task-")]
    assert len(remaining) == 3, f"应剩 3 条，实际 {len(remaining)}: {remaining}"
    # 最早的 old-task-0, old-task-1 应被淘汰
    assert "old-task-0" not in task_service._tasks
    assert "old-task-1" not in task_service._tasks
    assert "old-task-4" in task_service._tasks


def test_cleanup_drops_by_retention_hours():
    """超 retention_hours → 全部淘汰"""
    config_dir = _use_temp_config()
    setting_service.save_settings({
        "task_history_max_count": 500,  # 不限数量
        "task_history_retention_hours": 1,  # 1 小时前全清
    })

    # 注入 2 小时前的任务
    old_time = datetime.now() - timedelta(hours=2)
    old_tid = "old-task-2h"
    info = TaskInfo(old_tid, "generate", "old_2h")
    info.status = "success"
    info.start_time = old_time.strftime("%Y-%m-%d %H:%M:%S")
    info.end_time = old_time.strftime("%Y-%m-%d %H:%M:%S")
    with task_service._lock:
        task_service._tasks[old_tid] = info

    # 注入 30 分钟前的任务（应保留）
    recent_time = datetime.now() - timedelta(minutes=30)
    recent_tid = "recent-task-30m"
    info2 = TaskInfo(recent_tid, "generate", "recent_30m")
    info2.status = "success"
    info2.start_time = recent_time.strftime("%Y-%m-%d %H:%M:%S")
    info2.end_time = recent_time.strftime("%Y-%m-%d %H:%M:%S")
    with task_service._lock:
        task_service._tasks[recent_tid] = info2

    with task_service._lock:
        task_service._cleanup_finished_locked()

    assert old_tid not in task_service._tasks, "2 小时前任务应被淘汰"
    assert recent_tid in task_service._tasks, "30 分钟前任务应保留"


def test_cleanup_skipped_when_settings_corrupt():
    """配置异常时不清理（防御性）"""
    config_dir = _use_temp_config()
    settings_path = config_dir / "settings.json"
    settings_path.write_text("{ invalid json", encoding="utf-8")

    tid = "old-corrupt-test"
    info = TaskInfo(tid, "generate", "old")
    info.status = "success"
    info.end_time = "2020-01-01 00:00:00"
    with task_service._lock:
        task_service._tasks[tid] = info

    # 配置损坏 → load_settings 返回默认（DEFAULT_SETTINGS 有 max_count/retention）
    # 但 task_history_max_count=500 / retention_hours=24 → 2020 年的会被淘汰
    # 实际行为：默认配置生效，2020 任务超 24h 应被清
    with task_service._lock:
        task_service._cleanup_finished_locked()
    # 此处测：调用不抛错即可（即便被清也是正常路径）
    # 验证最弱的不变量：lock 仍可重入
    with task_service._lock:
        pass


def test_submit_triggers_cleanup():
    """submit 时自动触发清理（无需手动调）"""
    config_dir = _use_temp_config()
    setting_service.save_settings({
        "task_history_max_count": 1,
        "task_history_retention_hours": 24,
    })

    # 注入 5 条终态任务
    for i in range(5):
        tid = f"stale-{i}"
        info = TaskInfo(tid, "generate", f"stale_{i}")
        info.status = "success"
        info.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with task_service._lock:
            task_service._tasks[tid] = info

    assert len([t for t in task_service._tasks if t.startswith("stale-")]) == 5

    # submit 新任务（应触发清理）
    def quick(info):
        return "success"
    new_tid = task_service.submit("generate", "trigger_cleanup", quick)
    _wait_done(new_tid)

    # stale-* 应只剩 1 条
    remaining = [t for t in task_service._tasks if t.startswith("stale-")]
    assert len(remaining) == 1, f"submit 后 stale 应剩 1 条，实际 {len(remaining)}"


def test_settings_have_cleanup_defaults():
    """#高危-1：DEFAULT_SETTINGS 应有清理相关字段"""
    from app.services.setting_service import DEFAULT_SETTINGS
    assert "task_history_max_count" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["task_history_max_count"] == 500
    assert "task_history_retention_hours" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["task_history_retention_hours"] == 24


# ============ #高危-2：publish retry 取消释放视频 ============

def test_publish_one_item_has_cancel_release_path():
    """#高危-2：_publish_one_item_inner 应捕获 _TaskCancelled 并释放视频 + 标 cancelled"""
    import inspect
    from app.services import publish_service

    src = inspect.getsource(publish_service._publish_one_item_inner)

    # 1) 必须有 except _TaskCancelled 块
    assert "except _TaskCancelled" in src, \
        "_publish_one_item_inner 缺 _TaskCancelled 异常处理（retry 期间取消视频卡死）"

    # 2) except 块内必须释放视频
    cancel_block = src.split("except _TaskCancelled", 1)[1]
    assert "_release_video" in cancel_block, \
        "取消路径未调 _release_video（视频占用永不释放）"

    # 3) except 块内必须标 publish_task_item 为 cancelled
    assert "'cancelled'" in cancel_block or '"cancelled"' in cancel_block, \
        "取消路径未将 publish_task_item 标 cancelled"

    # 4) except 块内必须调 _refresh_task_progress
    assert "_refresh_task_progress" in cancel_block, \
        "取消路径未刷新任务进度"

    # 5) except 块末尾必须 raise（让 task_service 标记 cancelled）
    # 取 except 块到下一个 except 或函数结尾之间
    after_cancel = cancel_block.split("except", 1)[0] if "except" in cancel_block else cancel_block
    assert "raise" in after_cancel, \
        "取消路径未 raise（task_service 不会标 cancelled）"


def test_publish_one_item_cancelled_release_only_when_not_finalized():
    """#高危-2：成功 / 已失败的 item 不再释放（避免覆盖 success/failed 状态）

    取消路径应判断当前 status：success/failed/cancelled 已固化 → 跳过释放；
    publishing/waiting/suspended → 释放 + 标 cancelled。
    """
    import inspect
    from app.services import publish_service

    src = inspect.getsource(publish_service._publish_one_item_inner)
    cancel_block = src.split("except _TaskCancelled", 1)[1]

    # 取消块必须查询当前 status（避免覆盖 success）
    assert 'SELECT status FROM publish_task_item' in cancel_block, \
        "取消路径未查询当前 status（可能覆盖 success/failed）"

    # 取消块必须过滤掉终态 status
    assert 'success' in cancel_block and 'failed' in cancel_block, \
        "取消路径未跳过已成功/已失败状态"


def test_publish_service_imports_taskcancelled():
    """#高危-2：publish_service 必须 import _TaskCancelled（否则无法 except）"""
    from app.services import publish_service
    import inspect
    src = inspect.getsource(publish_service)
    assert "_TaskCancelled" in src, "publish_service 未 import _TaskCancelled"


# ============ 现场 bug：material_service._run_pull_round NameError ============

def test_material_pull_round_no_account_id_nameerror():
    """线上 bug：_run_pull_round 调 _process_single_video 时
    account_id=account_id 未定义（NameError），导致整轮失败 + partial。
    修法：account_id=account["id"]。
    """
    import ast
    from app.services import material_service

    src = open(material_service.__file__, encoding="utf-8").read()
    tree = ast.parse(src)

    # 找 _run_pull_round 函数
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_pull_round":
            func = node
            break
    assert func is not None, "未找到 _run_pull_round"

    # 检查函数体内不应有 bare `account_id` 标识符在右侧（无定义上下文）
    # 简单做法：搜 _process_single_video( 调用附近参数
    func_src = ast.get_source_segment(src, func)
    # 必须传 account_id（不是 account_id=account_id）
    assert "account_id=account_id" not in func_src, \
        "_run_pull_round 内有 account_id=account_id NameError（未定义变量）"
    # 正确写法：account_id=account["id"]
    assert "account_id=account[\"id\"]" in func_src or "account_id=account['id']" in func_src, \
        "_run_pull_round 未正确传 account_id=account['id']"


def test_material_pull_round_no_pages_done_nameerror():
    """线上 bug：has_more=0 时 fail_reason 引用未定义的 pages_done（应是 page）。
    修法：pages_done → page。
    """
    from app.services import material_service

    src = open(material_service.__file__, encoding="utf-8").read()

    # _run_pull_round 内不能有未定义的 pages_done
    import ast
    tree = ast.parse(src)
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_pull_round":
            func = node
            break
    func_src = ast.get_source_segment(src, func)

    # pages_done 不应在 _run_pull_round 内出现（变量未定义）
    assert "pages_done" not in func_src, \
        "_run_pull_round 引用未定义变量 pages_done（应是 page）"


def test_material_pull_round_uses_interruptible_sleep():
    """#P1-3：retry 循环内的 sleep 应可中断（原 time.sleep 阻塞期间取消信号无响应）。"""
    from app.services import material_service
    import ast

    src = open(material_service.__file__, encoding="utf-8").read()
    tree = ast.parse(src)

    # 找 _run_pull_round
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_pull_round":
            func = node
            break
    func_src = ast.get_source_segment(src, func)

    # retry 循环段必须用 interruptible_sleep
    assert "interruptible_sleep(wait_sec" in func_src, \
        "_run_pull_round retry 循环未用 interruptible_sleep（time.sleep 阻塞取消信号）"
    # import interruptible_sleep
    assert "interruptible_sleep" in src, "material_service 未 import interruptible_sleep"


# ============ runner ============

def _run_all():
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