# -*- coding: utf-8 -*-
"""#413 发布执行层适配：_write_publish_record 写入 intro/topics/declaration/allow_download。

通过 mock get_douyin_client 让 _publish_one_item_inner 走成功路径，
断言 publish_record 行含新字段（intro_snapshot / topics_snapshot /
declaration / allow_download）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import database as db_module
from app.db import migrations
from app.services import publish_service, setting_service


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="dt_pubrec_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    db_path = tmp / "db" / "short_video_tools.db"
    db_module.init_db(db_path)
    # 让 _video_abs_path 能解析 data 目录
    setting_service.init_data_dir(tmp / "data")
    v1_ddl = next(d for v, d in migrations.MIGRATIONS if v == 1)
    db_module.db.executescript(v1_ddl)
    db_module.db.set_user_version(33)
    for fn in ("_apply_v34", "_apply_v35", "_apply_v36", "_apply_v37", "_apply_v38"):
        getattr(migrations, fn)(db_module.db)
    db_module.db.set_user_version(38)

    # 1 账号 + 1 项目 + 1 门店 + 1 成品视频
    db_module.db.insert("account", {
        "id": "accA", "douyin_id": "dy_a", "nickname": "账号A", "remark": "账号A",
        "cookie_encrypted": "x", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("project", {
        "id": "projA", "title": "项目A", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("shop", {
        "id": "shopA1", "poi_id": "poi_a1", "name": "门店A1",
        "category": "美食", "create_time": "2026-09-22 00:00:00",
        "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("generated_video", {
        "id": "v1", "project_id": "projA",
        "file_path": "v/v1.mp4", "combination_key": "v1_key",
        "status": "occupied",
        "create_time": "2026-09-22 00:00:00",
    })
    # 1 条任务 + 1 条 item（含 #413 新字段）
    db_module.db.insert("publish_task", {
        "id": "task1", "task_name": "T", "project_ids_json": '["projA"]',
        "account_ids_json": '["accA"]', "shop_ids_json": '["shopA1"]',
        "per_account_count": 1, "start_time": "2026-09-22 07:00:00",
        "end_time": "2026-09-22 22:00:00",
        "interval_minutes": 60, "shop_assign_mode": "round",
        "status": "running", "declaration": "ai_generated",
        "allow_download": 0,
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    return db_path


def test_write_publish_record_includes_v38_fields() -> None:
    """_write_publish_record 写入 intro/topics/declaration/allow_download。"""
    _fresh_db()
    task = db_module.db.query_one("SELECT id FROM publish_task LIMIT 1")
    db_module.db.insert("publish_task_item", {
        "task_id": task["id"], "plan_time": "2026-09-22 07:00:00",
        "account_id": "accA", "project_id": "projA",
        "video_id": "v1", "shop_id": "shopA1",
        "intro_id": "intro_test",
        "intro_snapshot": "项目A简介0",
        "topics_snapshot": json.dumps(["#美食", "#探店"], ensure_ascii=False),
        "declaration": "ai_generated",
        "allow_download": 0,
        "status": "publishing",
    })
    item = db_module.db.query_one("SELECT * FROM publish_task_item LIMIT 1")
    account = db_module.db.query_one("SELECT * FROM account WHERE id=?", ("accA",))
    shop = db_module.db.query_one("SELECT * FROM shop WHERE id=?", ("shopA1",))
    video = db_module.db.query_one("SELECT * FROM generated_video WHERE id=?", ("v1",))

    rid = publish_service._write_publish_record(
        item, account, shop,
        title="标题 #美食",
        topic="#美食",
        video=video, video_title="项目A",
        online_video_id="onl_123")
    assert rid
    rec = db_module.db.query_one("SELECT * FROM publish_record WHERE id=?", (rid,))
    assert rec["intro_snapshot"] == "项目A简介0"
    assert json.loads(rec["topics_snapshot"]) == ["#美食", "#探店"]
    assert rec["declaration"] == "ai_generated"
    assert rec["allow_download"] == 0


def test_publish_one_item_writes_record_with_v38_fields() -> None:
    """_publish_one_item_inner 成功路径：publish_record 含 #413 字段。

    手工 monkeypatch（不依赖 pytest）：临时替换 publish_service.get_douyin_client
    与 account_service.get_cookie，跑完恢复。
    """
    _fresh_db()
    task = db_module.db.query_one("SELECT id FROM publish_task LIMIT 1")
    # #455 修：用相对时间（10 年后）避免未来日期漂移；_publish_one_item_inner 顶部
    # 会校验 plan_time 是否过期，绝对硬编码 2099 在 2099+1 年测试直接全失败
    future_dt = datetime.now() + timedelta(days=365 * 10)
    future_plan_time = future_dt.strftime("%Y-%m-%d %H:%M:%S")
    db_module.db.insert("publish_task_item", {
        "task_id": task["id"], "plan_time": future_plan_time,
        "account_id": "accA", "project_id": "projA",
        "video_id": "v1", "shop_id": "shopA1",
        "intro_id": "intro_test",
        "intro_snapshot": "项目A简介0",
        "topics_snapshot": json.dumps(["#美食"], ensure_ascii=False),
        "declaration": "ai_generated",
        "allow_download": 1,
        "status": "publishing",
    })
    item = db_module.db.query_one("SELECT * FROM publish_task_item LIMIT 1")

    class FakeClient:
        # #455：publish_video 签名含 account_id/schedule/allow_save kwarg；mock 用 **kwargs 兼容
        def publish_video(self, cookie, video, title, topic, shop_name, **kwargs):
            return {"success": True, "online_video_id": "online_xyz"}

    orig_client = publish_service.get_douyin_client
    orig_cookie = publish_service.account_service.get_cookie
    try:
        publish_service.get_douyin_client = lambda: FakeClient()
        publish_service.account_service.get_cookie = lambda acc_id: "fake_cookie"

        info = SimpleNamespace(start_ts=None, progress="", message="",
                                cancel_requested=False)
        result = publish_service._publish_one_item_inner(item["id"], info)
    finally:
        publish_service.get_douyin_client = orig_client
        publish_service.account_service.get_cookie = orig_cookie

    assert result == "success"

    item_after = db_module.db.query_one(
        "SELECT status, publish_record_id FROM publish_task_item WHERE id=?", (item["id"],))
    assert item_after["status"] == "success"
    assert item_after["publish_record_id"]

    rec = db_module.db.query_one(
        "SELECT * FROM publish_record WHERE id=?", (item_after["publish_record_id"],))
    assert rec["intro_snapshot"] == "项目A简介0"
    assert json.loads(rec["topics_snapshot"]) == ["#美食"]
    assert rec["declaration"] == "ai_generated"
    assert rec["allow_download"] == 1


if __name__ == "__main__":
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