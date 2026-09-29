# -*- coding: utf-8 -*-
"""#413 intro / project_shop service 单测：CRUD + 顺序覆盖语义。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import database as db_module
from app.db import migrations
from app.services import publish_intro_service


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="dt_intro_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    db_path = tmp / "db" / "short_video_tools.db"
    db_module.init_db(db_path)
    v1_ddl = next(d for v, d in migrations.MIGRATIONS if v == 1)
    db_module.db.executescript(v1_ddl)
    db_module.db.set_user_version(33)
    for fn in ("_apply_v34", "_apply_v35", "_apply_v36", "_apply_v37", "_apply_v38"):
        getattr(migrations, fn)(db_module.db)
    db_module.db.set_user_version(38)
    # 后续迁移（含 v39+ intro/project_shop 字段、v42 started_at、v43 数据中心移除）走完整 migrate
    migrations.migrate(db_module.db)
    # 至少一个项目 + 至少 3 个门店（用于绑定测试）
    # #444：list_project_shops 过滤 is_added_to_library=1 AND is_cps=1，shop fixture 必须满足
    db_module.db.insert("project", {
        "id": "p1", "title": "项目1", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    for sid, name in (("s1", "门店A"), ("s2", "门店B"), ("s3", "门店C")):
        db_module.db.insert("shop", {
            "id": sid, "poi_id": f"poi_{sid}", "name": name,
            "is_added_to_library": 1, "is_cps": 1,
            "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
        })
    return db_path


def test_intro_crud_round_trip() -> None:
    _fresh_db()
    intro = publish_intro_service.create_intro(
        project_id="p1", content="测试简介 {门店名}", topics=["#美食", "#探店"])
    assert intro["id"]
    rows = publish_intro_service.list_intros("p1")
    assert len(rows) == 1
    # #415：topics 合并到 content 末尾；topics 从 content 正则提取
    assert rows[0]["content"] == "测试简介 {门店名} #美食 #探店"
    assert sorted(rows[0]["topics"]) == ["#探店", "#美食"]

    # 更新
    n = publish_intro_service.update_intro(intro["id"], content="新简介", topics=["#新"])
    assert n == 1
    rows = publish_intro_service.list_intros("p1")
    assert rows[0]["content"] == "新简介 #新"
    assert rows[0]["topics"] == ["#新"]

    # 删除
    n = publish_intro_service.delete_intro(intro["id"])
    assert n == 1
    rows = publish_intro_service.list_intros("p1")
    assert len(rows) == 0


def test_intro_empty_content_rejected() -> None:
    _fresh_db()
    try:
        publish_intro_service.create_intro(project_id="p1", content="", topics=[])
    except ValueError:
        return
    raise AssertionError("期望 ValueError")


def test_project_shop_set_overwrites_in_order() -> None:
    """set_project_shops 全量覆盖，sort_order 即勾选顺序。"""
    _fresh_db()
    # 第一次：s1, s2
    n = publish_intro_service.set_project_shops("p1", ["s1", "s2"])
    assert n == 2
    rows = publish_intro_service.list_project_shops("p1")
    assert [r["shop_id"] for r in rows] == ["s1", "s2"]
    assert [r["sort_order"] for r in rows] == [0, 1]

    # 第二次：s3, s1, s2（顺序变了 → sort_order 重排）
    n = publish_intro_service.set_project_shops("p1", ["s3", "s1", "s2"])
    assert n == 3
    rows = publish_intro_service.list_project_shops("p1")
    assert [r["shop_id"] for r in rows] == ["s3", "s1", "s2"]
    assert [r["sort_order"] for r in rows] == [0, 1, 2]

    # 第三次：清空只留 s1
    n = publish_intro_service.set_project_shops("p1", ["s1"])
    assert n == 1
    rows = publish_intro_service.list_project_shops("p1")
    assert [r["shop_id"] for r in rows] == ["s1"]


def test_projects_with_shops_count() -> None:
    _fresh_db()
    publish_intro_service.set_project_shops("p1", ["s1", "s2"])
    rows = publish_intro_service.list_projects_with_shops()
    assert len(rows) == 1
    assert rows[0]["shop_count"] == 2


def test_imports() -> None:
    """模块导入无错（路由注册的最小烟测）。"""
    from app.api import publish_intro  # noqa: F401
    assert publish_intro.router


if __name__ == "__main__":
    import inspect
    funcs = [(n, o) for n, o in globals().items() if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in funcs:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{'OK' if failed == 0 else 'FAIL'} {len(funcs) - failed}/{len(funcs)}")
    sys.exit(0 if failed == 0 else 1)