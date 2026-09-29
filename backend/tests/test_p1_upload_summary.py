# -*- coding: utf-8 -*-
"""P1-5 测试：upload_service._update_task_summary_from_db 从 DB 实时统计。

使用临时 SQLite DB（仅建需要的 upload_task / upload_item 表），不依赖 migrations。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import database as db_module
from app.db.utils import new_id, now_str
from app.services.upload_service import _update_task_summary_from_db


def _setup_temp_db():
    """建临时 DB + upload_task/upload_item 表，返回 Database 实例。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = db_module.init_db(Path(tmp.name))
    with db.transaction():
        db.execute("""
            CREATE TABLE upload_task (
                id TEXT PRIMARY KEY,
                status TEXT,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                message TEXT,
                update_time TEXT,
                deleted INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE upload_item (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                status TEXT,
                deleted INTEGER DEFAULT 0
            )
        """)
    return db


def _make_task(db, status="running", success=0, failed=0):
    tid = new_id()
    db.insert("upload_task", {
        "id": tid, "status": status,
        "success_count": success, "failed_count": failed,
        "message": "", "update_time": now_str(),
    })
    return tid


def _add_items(db, task_id, statuses: list):
    for s in statuses:
        db.insert("upload_item", {
            "id": new_id(), "task_id": task_id, "status": s,
        })


# ============ tests ============

def test_all_success():
    """全 success → status=success"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _add_items(db, tid, ["success"] * 5)
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["status"] == "success", f"实际 {row['status']}"
    assert row["success_count"] == 5
    assert row["failed_count"] == 0


def test_all_failed():
    """全 failed → status=failed"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _add_items(db, tid, ["failed"] * 3)
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["status"] == "failed"
    assert row["failed_count"] == 3


def test_partial_mix():
    """成功 + 失败 混合 → status=partial"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _add_items(db, tid, ["success", "failed", "success", "failed", "success"])
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["status"] == "partial"
    assert row["success_count"] == 3
    assert row["failed_count"] == 2


def test_has_cancelled():
    """任意 cancelled → status=cancelled"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _add_items(db, tid, ["success", "cancelled"])
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["status"] == "cancelled"


def test_deleted_items_excluded():
    """deleted=1 的 item 不计入"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _add_items(db, tid, ["success", "failed"])
    # 额外加一个 deleted=1 的 success（不应计入）
    db.insert("upload_item", {
        "id": new_id(), "task_id": tid, "status": "success", "deleted": 1,
    })
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["status"] == "partial"
    assert row["success_count"] == 1
    assert row["failed_count"] == 1


def test_no_items_noop():
    """无 item 时函数直接返回，不抛错"""
    db = _setup_temp_db()
    tid = _make_task(db)
    _update_task_summary_from_db(tid)  # 不应抛错
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    # 状态保持原值 running（未被覆盖）
    assert row["status"] == "running"


def test_retry_corrects_count():
    """#P1-5 核心场景：失败 item 重试成功后计数应被纠正
    之前用 hardcode (1, 0) 多次重试会累加错乱；现在从 DB 真实统计
    """
    db = _setup_temp_db()
    tid = _make_task(db)
    # 模拟已存在 1 success + 1 failed
    _add_items(db, tid, ["success", "failed"])
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["success_count"] == 1
    assert row["failed_count"] == 1
    # 模拟重试：把那条 failed 改成 success
    items = db.query_all("SELECT id FROM upload_item WHERE task_id=? AND status='failed'", (tid,))
    db.execute("UPDATE upload_item SET status='success' WHERE id=?", (items[0]["id"],))
    # 再调一次统计——不应再累加为 (2, 1)，而应正确反映 DB 真实值 (2, 0)
    _update_task_summary_from_db(tid)
    row = db.query_one("SELECT * FROM upload_task WHERE id=?", (tid,))
    assert row["success_count"] == 2, f"期望 2 实际 {row['success_count']}"
    assert row["failed_count"] == 0
    assert row["status"] == "success"


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
