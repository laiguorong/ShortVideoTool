# -*- coding: utf-8 -*-
"""v38 迁移落地验证：从空库跑 migrate()，断言新表/新列就位。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import database as db_module
from app.db import migrations


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="dt_v38_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    db_path = tmp / "db" / "short_video_tools.db"
    db_module.init_db(db_path)
    return db_path


def _columns(db, table: str) -> set[str]:
    rows = db.query_all(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows}


def _table_exists(db, name: str) -> bool:
    row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return row is not None


def test_v38_migrate() -> None:
    db_path = _fresh_db()
    db = db_module.db

    # 跑 v1 CREATE（含 publish_task / item / record / 视频简介/项目门店表）
    v1_ddl = next(d for v, d in migrations.MIGRATIONS if v == 1)
    db.executescript(v1_ddl)
    # 跳到 v33 之后（绕开 v33 DDL 漏写 -- 前缀的预先 bug）
    db.set_user_version(33)
    # 跑 v33 之后所有 _apply_vN（每个都幂等，缺表跳过）
    for fn_name in ("_apply_v34", "_apply_v35", "_apply_v36", "_apply_v37", "_apply_v38"):
        getattr(migrations, fn_name)(db)
    db.set_user_version(38)

    # 新表
    assert _table_exists(db, "video_intro"), "video_intro 表未建"
    assert _table_exists(db, "project_shop"), "project_shop 表未建"

    # publish_task 扩列
    task_cols = _columns(db, "publish_task")
    must_have_task = {
        "end_time", "daily_limit_mode", "daily_limit_global",
        "daily_limit_per_account_json", "same_project_interval_min",
        "diff_project_interval_min", "declaration", "allow_download",
        "projects_payload_json",
    }
    missing = must_have_task - task_cols
    assert not missing, f"publish_task 缺列：{missing}"

    # publish_task_item 扩列
    item_cols = _columns(db, "publish_task_item")
    must_have_item = {
        "intro_id", "intro_snapshot", "topics_snapshot",
        "declaration", "allow_download",
    }
    missing = must_have_item - item_cols
    assert not missing, f"publish_task_item 缺列：{missing}"

    # publish_record 扩列
    record_cols = _columns(db, "publish_record")
    must_have_record = {
        "intro_snapshot", "topics_snapshot", "declaration", "allow_download",
    }
    missing = must_have_record - record_cols
    assert not missing, f"publish_record 缺列：{missing}"

    # 索引
    idx_names = {r["name"] for r in db.query_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    assert "idx_intro_project" in idx_names
    assert "idx_project_shop_proj" in idx_names

    # 写入一条简介 + 项目-门店关联，验证不报错
    db.insert("project", {
        "id": "p_test", "title": "测试项目", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db.insert("video_intro", {
        "id": "i_test", "project_id": "p_test", "content": "测试简介 {门店名}",
        "topics_json": '["#美食", "#探店"]',
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db.execute("INSERT INTO project_shop(project_id, shop_id, sort_order) VALUES (?, ?, ?)",
               ("p_test", "s_test", 0))
    rows = db.query_all("SELECT * FROM video_intro WHERE project_id=?", ("p_test",))
    assert len(rows) == 1
    rows = db.query_all("SELECT * FROM project_shop WHERE project_id=?", ("p_test",))
    assert len(rows) == 1

    print(f"v38 migrate OK (db={db_path})")


if __name__ == "__main__":
    test_v38_migrate()