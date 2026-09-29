# -*- coding: utf-8 -*-
"""#413 草稿/预览/确认/复制 端到端单测：对接排期算法 + 数据库。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import database as db_module
from app.db import migrations
from app.services import publish_service, publish_intro_service


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="dt_draft_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    db_path = tmp / "db" / "short_video_tools.db"
    db_module.init_db(db_path)
    v1_ddl = next(d for v, d in migrations.MIGRATIONS if v == 1)
    db_module.db.executescript(v1_ddl)
    db_module.db.set_user_version(33)
    for fn in ("_apply_v34", "_apply_v35", "_apply_v36", "_apply_v37", "_apply_v38"):
        getattr(migrations, fn)(db_module.db)
    db_module.db.set_user_version(38)
    # 后续迁移（含 v42 started_at / v43 移除数据中心）走完整 migrate
    migrations.migrate(db_module.db)

    # 2 个账号（正常）
    db_module.db.insert("account", {
        "id": "accA", "douyin_id": "dy_a", "nickname": "账号A", "remark": "账号A",
        "cookie_encrypted": "x", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("account", {
        "id": "accB", "douyin_id": "dy_b", "nickname": "账号B", "remark": "账号B",
        "cookie_encrypted": "x", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    # 2 个项目
    db_module.db.insert("project", {
        "id": "projA", "title": "项目A", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("project", {
        "id": "projB", "title": "项目B", "status": "normal",
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    # 8 个门店（A4 + B4）
    for i in range(1, 5):
        for letter, proj in (("A", "projA"), ("B", "projB")):
            sid = f"shop_{letter}{i}"
            db_module.db.insert("shop", {
                "id": sid, "poi_id": f"poi_{sid}", "name": f"{letter}门店{i}",
                "is_added_to_library": 1, "is_cps": 1,  # #444：必须加库 + 有佣金才进入发布
                "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
            })
    # 项目-门店绑定
    publish_intro_service.set_project_shops("projA", ["shop_A1", "shop_A2", "shop_A3", "shop_A4"])
    publish_intro_service.set_project_shops("projB", ["shop_B1", "shop_B2", "shop_B3", "shop_B4"])
    # 简介：项目A 5 条，项目B 5 条
    for i in range(5):
        publish_intro_service.create_intro("projA", f"项目A简介{i}", [f"#A{i}"])
        publish_intro_service.create_intro("projB", f"项目B简介{i}", [f"#B{i}"])
    return db_path


def _draft_payload(per_account: bool = False) -> tuple:
    projects_payload = [
        {"project_id": "projA"},
        {"project_id": "projB"},
    ]
    if per_account:
        limits = [("accA", 4), ("accB", 6)]
        return projects_payload, "per_account", 0, limits
    return projects_payload, "global", 5, []


def test_draft_preview_confirm_end_to_end() -> None:
    from unittest.mock import patch
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload(per_account=False)

    draft = publish_service.save_draft(
        task_name="测试任务", projects_payload=projects_payload,
        account_ids=["accA", "accB"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    task_id = draft["task_id"]

    # 草稿应为 draft 态
    task = db_module.db.query_one("SELECT * FROM publish_task WHERE id=?", (task_id,))
    assert task["status"] == "draft"

    # 预览
    preview = publish_service.preview_task(task_id)
    assert not preview["overflow"]
    assert preview["stats"]["total"] == 10  # 2 账号 × 5 条
    assert preview["stats"]["by_account"] == {"accA": 5, "accB": 5}

    # 确认前需要预生成成品（占 idle）
    # 2 账号 × 5 条/账号 = 10 条/项目 共 20 条
    for proj_id in ("projA", "projB"):
        for i in range(10):
            db_module.db.insert("generated_video", {
                "id": f"v_{proj_id}_{i}", "project_id": proj_id,
                "file_path": f"v/{proj_id}_{i}.mp4",
                "combination_key": f"{proj_id}_{i}",
                "status": "idle",
                "create_time": f"2026-09-22 {i % 24:02d}:00:00",
            })

    # #confirm-kickoff：拦截 task_service.submit，避免 mock 发布 worker 误跑
    # 后续明细 status 断言才稳定（首槽被原子抢为 publishing，剩余 waiting）
    with patch.object(publish_service.task_service, "submit",
                      lambda task_type, name, func: None):
        confirmed = publish_service.confirm_task(task_id)
    assert confirmed["running"] is True
    assert confirmed["item_count"] == 10

    # 状态翻转
    task = db_module.db.query_one("SELECT * FROM publish_task WHERE id=?", (task_id,))
    assert task["status"] == "running"

    # 明细数 = 10；confirm 后被 dispatch_all_waiting 全部抢为 publishing
    items = db_module.db.query_all(
        "SELECT * FROM publish_task_item WHERE task_id=? ORDER BY plan_time ASC, id ASC",
        (task_id,))
    assert len(items) == 10
    # 全部明细 confirm 后立即 publishing，不再依赖 poller 按 plan_time 触发
    for it in items:
        assert it["status"] == "publishing", (
            f"#confirm-kickoff-2：全部明细应立即 publishing，实际 {it['status']!r} "
            f"plan_time={it['plan_time']!r}"
        )
        assert it["declaration"] == "ai_generated"
        assert it["allow_download"] == 0
        assert it["intro_snapshot"]
        topics = json.loads(it["topics_snapshot"])
        assert isinstance(topics, list)
    # 成品被占
    occ = db_module.db.query_one(
        "SELECT COUNT(*) AS c FROM generated_video WHERE status='occupied'")["c"]
    assert occ == 10


def test_preview_overflow_reports() -> None:
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    draft = publish_service.save_draft(
        task_name="溢出任务", projects_payload=projects_payload,
        account_ids=["accA", "accB"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 07:30:00",  # 30min 窗
    )
    # #calc_mode 终版：window<interval → ScheduleConfig.__post_init__ 抛 ValueError
    try:
        publish_service.preview_task(draft["task_id"])
    except ValueError as e:
        assert "均衡间隔" in str(e) or "连 1 条都排不下" in str(e)
        return
    raise AssertionError("期望 ValueError，未抛")


def test_duplicate_returns_payload_no_db_write() -> None:
    """#453：duplicate_task 返回等价 DraftRequest payload，不写库；任何状态均可调用。

    旧行为：save_draft 落新 draft（task_id + status='draft'）
    新行为：返回 {payload: {...}} 给前端 wizard 回显，确认时才走 confirm_task_direct 落库
    """
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    src = publish_service.save_draft(
        task_name="原任务", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
        declaration="none", allow_download=True,
    )
    # 计数：源 task 落库后总任务数 = 1
    before = db_module.db.query_one("SELECT COUNT(*) AS c FROM publish_task")["c"]
    assert before == 1
    # 复制：返回 payload
    dup = publish_service.duplicate_task(src["task_id"])
    assert "payload" in dup
    payload = dup["payload"]
    # payload 字段断言
    assert payload["task_name"].endswith("_副本")
    assert payload["declaration"] == "none"
    assert payload["allow_download"] is True
    assert payload["account_ids"] == ["accA"]
    assert payload["start_time"] == "2026-09-22 07:00:00"
    assert payload["end_time"] == "2026-09-22 22:00:00"
    assert payload["project_source"] == "project"
    # 复制后 DB 任务数仍 = 1（不落库）
    after = db_module.db.query_one("SELECT COUNT(*) AS c FROM publish_task")["c"]
    assert after == 1, f"duplicate_task 不应写库，复制后任务数仍应为 1，实际 {after}"


def test_duplicate_allows_running_status() -> None:
    """#453：不限状态——running 任务也能复制（返回 payload）。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    src = publish_service.save_draft(
        task_name="running src", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
        declaration="none", allow_download=True,
    )
    # 手动改成 running 模拟
    db_module.db.execute("UPDATE publish_task SET status='running' WHERE id=?", (src["task_id"],))
    dup = publish_service.duplicate_task(src["task_id"])
    assert "payload" in dup
    # 仍不写库
    cnt = db_module.db.query_one("SELECT COUNT(*) AS c FROM publish_task")["c"]
    assert cnt == 1


def test_duplicate_allows_finished_and_cancelled() -> None:
    """#453 nit：finished / cancelled 状态复制覆盖（之前 status 守卫挡了）。"""
    for status in ("finished", "cancelled"):
        _fresh_db()
        projects_payload, mode, global_lim, per_acc = _draft_payload()
        src = publish_service.save_draft(
            task_name=f"{status} src", projects_payload=projects_payload,
            account_ids=["accA"], daily_limit_mode=mode,
            daily_limit_global=global_lim, daily_limit_per_account=per_acc,
            start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
            declaration="none", allow_download=True,
        )
        db_module.db.execute(
            "UPDATE publish_task SET status=? WHERE id=?", (status, src["task_id"]))
        dup = publish_service.duplicate_task(src["task_id"])
        assert "payload" in dup, f"{status} 任务复制应返回 payload"
        cnt = db_module.db.query_one("SELECT COUNT(*) AS c FROM publish_task")["c"]
        assert cnt == 1, f"{status} 复制不应写库"


def test_delete_draft_releases_generated_video() -> None:
    """#critical：删除 draft 时释放 publish_task_item 引用的 generated_video 占用。

    场景：confirm_task_direct 落 publish_task_item + UPDATE generated_video.status='occupied'，
    若任务回滚到 draft 状态后被用户删除，必须把 video 还回 idle，否则 occupied 永不归还。
    """
    _fresh_db()
    # 准备一个 generated_video 占位
    vid = db_module.db.insert("generated_video", {
        "project_id": "pX", "file_path": "v.mp4", "status": "occupied",
        "combination_key": "test_combo_1",
    })
    # 用 save_draft 落 draft 任务（绕过所有必填字段），再手动 INSERT 一个 publish_task_item
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    src = publish_service.save_draft(
        task_name="to delete", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    task_id = src["task_id"]
    db_module.db.insert("publish_task_item", {
        "task_id": task_id, "video_id": vid, "account_id": "accA",
        "project_id": "pX", "shop_id": "", "plan_time": "2026-09-22 07:00:00",
        "status": "waiting",
    })
    # 删除
    publish_service.delete_task(task_id)
    # video 应回到 idle
    row = db_module.db.query_one("SELECT status FROM generated_video WHERE id=?", (vid,))
    assert row["status"] == "idle", f"delete_task 应释放 video 占位，实际 status={row['status']!r}"
    # publish_task_item 应硬删
    cnt = db_module.db.query_one(
        "SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=?", (task_id,))["c"]
    assert cnt == 0
    # publish_task 应软删
    cnt = db_module.db.query_one(
        "SELECT COUNT(*) AS c FROM publish_task WHERE id=? AND deleted=0", (task_id,))["c"]
    assert cnt == 0


def test_delete_draft_skips_video_dir_items_with_empty_video_id() -> None:
    """#critical：删除 draft 时 video_id 为空的明细（视频目录模式）应跳过释放，不报错。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    src = publish_service.save_draft(
        task_name="video_dir draft", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    task_id = src["task_id"]
    db_module.db.insert("publish_task_item", {
        "task_id": task_id, "video_id": "", "account_id": "accA",
        "project_id": "pX", "shop_id": "", "plan_time": "2026-09-22 07:00:00",
        "status": "waiting",
    })
    # 不应抛错
    publish_service.delete_task(task_id)
    cnt = db_module.db.query_one(
        "SELECT COUNT(*) AS c FROM publish_task WHERE id=? AND deleted=0", (task_id,))["c"]
    assert cnt == 0


def test_per_account_mode() -> None:
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload(per_account=True)
    draft = publish_service.save_draft(
        task_name="分账号任务", projects_payload=projects_payload,
        account_ids=["accA", "accB"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    preview = publish_service.preview_task(draft["task_id"])
    assert preview["stats"]["total"] == 4 + 6
    assert preview["stats"]["by_account"] == {"accA": 4, "accB": 6}


# ---------- R12 #413 向导回退保存覆盖 ----------


def test_save_draft_with_task_id_updates_existing_draft() -> None:
    """R12：传 task_id 时改为 UPDATE 旧 draft，不创建新任务。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    initial = publish_service.save_draft(
        task_name="原任务名", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
        declaration="ai_generated", allow_download=False,
    )
    tid = initial["task_id"]

    # 覆盖：task_name / daily_limit_global / declaration / allow_download
    updated = publish_service.save_draft(
        task_name="覆盖后名", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=8, daily_limit_per_account=per_acc,
        start_time="2026-09-22 08:00:00", end_time="2026-09-22 21:00:00",
        declaration="none", allow_download=True,
        task_id=tid,
    )
    assert updated["task_id"] == tid, "UPDATE 不应换 ID"

    detail = publish_service.get_task_detail(tid)
    assert detail["task_name"] == "覆盖后名"
    assert detail["daily_limit_global"] == 8
    assert detail["declaration"] == "none"
    assert detail["allow_download"] == 1
    assert detail["start_time"] == "2026-09-22 08:00:00"
    assert detail["end_time"] == "2026-09-22 21:00:00"


def test_save_draft_with_task_id_rejects_non_draft_status() -> None:
    """R12：UPDATE 仅允许 draft 状态；running/finished/cancelled 拒。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    draft = publish_service.save_draft(
        task_name="t", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    tid = draft["task_id"]
    # 模拟切到 running
    db_module.db.execute(
        "UPDATE publish_task SET status='running' WHERE id=?", (tid,))
    try:
        publish_service.save_draft(
            task_name="覆盖", projects_payload=projects_payload,
            account_ids=["accA"], daily_limit_mode=mode,
            daily_limit_global=global_lim, daily_limit_per_account=per_acc,
            start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
            task_id=tid,
        )
        assert False, "running 状态应被拒"
    except ValueError as e:
        assert "running" in str(e)


def test_save_draft_with_invalid_task_id_raises() -> None:
    """R12：不存在的 task_id 应报 ValueError。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    try:
        publish_service.save_draft(
            task_name="t", projects_payload=projects_payload,
            account_ids=["accA"], daily_limit_mode=mode,
            daily_limit_global=global_lim, daily_limit_per_account=per_acc,
            start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
            task_id="nonexistent_xyz",
        )
        assert False, "应报 ValueError"
    except ValueError as e:
        assert "不存在" in str(e)


# ---------- C3 状态过滤 ----------


def test_list_tasks_status_filter() -> None:
    """C3：list_tasks 按 status 过滤。"""
    _fresh_db()
    projects_payload, mode, global_lim, per_acc = _draft_payload()
    # 2 个 draft
    publish_service.save_draft(
        task_name="draft1", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )
    publish_service.save_draft(
        task_name="draft2", projects_payload=projects_payload,
        account_ids=["accA"], daily_limit_mode=mode,
        daily_limit_global=global_lim, daily_limit_per_account=per_acc,
        start_time="2026-09-22 07:00:00", end_time="2026-09-22 22:00:00",
    )

    all_res = publish_service.list_tasks(1, 50)
    assert all_res["total"] == 2

    draft_only = publish_service.list_tasks(1, 50, status="draft")
    assert draft_only["total"] == 2
    assert all(t["status"] == "draft" for t in draft_only["list"])

    running_only = publish_service.list_tasks(1, 50, status="running")
    assert running_only["total"] == 0


# ===== #calc_mode =====

def test_save_draft_default_mode_balanced() -> None:
    """#calc_mode：save_draft 不传新字段 → DEFAULT schedule_mode='balanced'。"""
    _fresh_db()
    r = publish_service.save_draft(
        task_name="t",
        projects_payload=[{"project_id": "projA", "shop_ids": ["shop_A1", "shop_A2"],
                            "intro_ids": ["intro_A0"]}],
        account_ids=["accA"],
        daily_limit_mode="global", daily_limit_global=2,
        daily_limit_per_account=[],
    )
    task = db_module.db.query_one("SELECT * FROM publish_task WHERE id=?", (r["task_id"],))
    assert task["schedule_mode"] == "balanced"
    assert task["fixed_interval_min"] == 10  # DEFAULT 兜底
    assert task["balanced_step_min"] == 60  # DEFAULT 兜底


def test_save_draft_fixed_mode_persists() -> None:
    """#calc_mode：save_draft(schedule_mode='fixed', fixed_interval_min=10) 落新字段。"""
    _fresh_db()
    r = publish_service.save_draft(
        task_name="t",
        projects_payload=[{"project_id": "projA", "shop_ids": ["shop_A1"],
                            "intro_ids": ["intro_A0"]}],
        account_ids=["accA"],
        daily_limit_mode="global", daily_limit_global=2,
        daily_limit_per_account=[],
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    task = db_module.db.query_one("SELECT * FROM publish_task WHERE id=?", (r["task_id"],))
    assert task["schedule_mode"] == "fixed"
    assert task["fixed_interval_min"] == 10
    assert task["balanced_step_min"] == 60


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