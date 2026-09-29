# -*- coding: utf-8 -*-
"""#186 回归测试：info.start_ts 与 elapsed 计算时钟必须一致。

原 bug：task_service._run 设 info.start_ts = monotonic()，但所有 service
用 time.time() - start_ts 计算耗时。两时钟混算导致 elapsed ≈ 当前 epoch - 系统启动时间
≈ 56 年，progress 显示 497098:31:37。

修法：info.start_ts 统一用 time.time()，所有 consumer 也用 time.time() - start_ts。
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.task_service import TaskInfo, task_service


# ============ 时钟一致性 ============

def test_start_ts_uses_wall_clock_not_monotonic():
    """#186：task_service 注入的 start_ts 应是 wall-clock time.time()，
    不可用 monotonic()（两者时钟基准不同，混算导致 56 年 elapsed）
    """
    from app.services import task_service as ts_module

    # 抓 _run 源码（直接 exec 拿源码更准——inline 闭包）
    # 用 inspect 找 _run 内嵌 _run 函数
    src = open(ts_module.__file__, encoding="utf-8").read()

    # 1) 必须用 time.time() 不用 monotonic
    assert "info.start_ts = _time.time()" in src, \
        "info.start_ts 应改用 time.time() 而非 monotonic（混算导致 56 年 elapsed）"
    # 2) 不应再用 monotonic 初始化 start_ts
    assert "info.start_ts = _time.monotonic()" not in src, \
        "info.start_ts 仍用 monotonic()，会与 time.time() 混算"


def test_elapsed_within_one_minute():
    """#186：worker 跑 1s 后 elapsed 应 < 60s，不应是 56 年"""
    barrier = {"released": False}

    def fn(info):
        # 模拟 1 秒工作
        import time as t
        t.sleep(1.0)
        # service 实际写 progress 时算的 elapsed
        elapsed = int(t.time() - info.start_ts)
        # 允许 0~3 秒抖动（sleep + 调度开销）
        assert 0 <= elapsed <= 3, \
            f"elapsed 应 ≤3 秒，实际 {elapsed}（说明 clock 混算）"
        return "success"

    tid = task_service.submit("generate", "elapsed_test", fn)
    # 等 worker 跑完
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        info = task_service.get_task(tid)
        if info and info["status"] == "success":
            break
        time.sleep(0.05)
    assert info["status"] == "success"


def test_fmt_hms_small_seconds():
    """#186：fmt 1s 应为 0:00:01，不会因 clock 混算变大"""
    from app.services.task_service import _fmt_hms
    assert _fmt_hms(1.0) == "0:00:01"
    assert _fmt_hms(59.0) == "0:00:59"
    assert _fmt_hms(60.0) == "0:01:00"
    assert _fmt_hms(3600.0) == "1:00:00"
    assert _fmt_hms(5025.0) == "1:23:45"


def test_all_services_use_time_time_consistently():
    """#186：所有 service 文件 elapsed 计算必须用 time.time()，与 info.start_ts 同源"""
    services_dir = Path(__file__).parent.parent / "app" / "services"
    services = [
        "creation_service.py",
        "material_service.py",
        "selection_service.py",
        "share_import_service.py",
        "publish_service.py",
        "stats_service.py",
        "upload_service.py",
    ]

    for svc_name in services:
        path = services_dir / svc_name
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        # 找 elapsed 计算模式：time.time() - start_ts / time.time() - round_start
        # 允许；但不能出现 time.monotonic() - start_ts
        assert "time.monotonic() - " not in src, \
            f"{svc_name} 出现 time.monotonic() - ... 与 info.start_ts（time.time()）混算"


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