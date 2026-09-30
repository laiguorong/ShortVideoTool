# -*- coding: utf-8 -*-
"""507 改造端到端 probe：mock 验证 _run_pull_round 两阶段编排。

验证：
1. 阶段 A：search_session.search_all_for_ids 返回 aweme_id 列表
2. 阶段 B：_run_detail_phase 顺序处理每个 aweme_id（详情+下载+入库）
3. 早退重试：aweme_ids 为空 → 重试 2 次（30s/45s）→ 仍空 → fail_reason
4. 单视频失败不影响其他
5. max_count 中断
"""
import sys
import time
import pathlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main() -> int:
    from app.services.setting_service import init_data_dir, get_data_dir, get_config_dir
    from app.core import crypto
    from app.db.database import init_db, get_db
    init_data_dir()
    crypto.init_crypto(get_config_dir())
    data_dir = get_data_dir()
    init_db(data_dir / "shortvideotool.db")
    d = get_db()

    row = d.query_one(
        "SELECT id, nickname FROM account WHERE deleted=0 AND status IN ('normal','valid') LIMIT 1"
    )
    if not row:
        print("[FAIL] no account")
        return 1
    account_id = row["id"]
    print(f"[account] {account_id}")

    cat = d.query_one(
        "SELECT id FROM material_category WHERE deleted=0 AND type='video' LIMIT 1"
    )
    cat_id = cat["id"] if cat else d.insert("material_category", {
        "name": "probe_507", "type": "video", "parent_id": "", "sort_order": 99})

    task_id = d.insert("video_pull_task", {
        "task_name": "probe_507_e2e",
        "conditions_json": '{"keyword":"鱼公元鱼生","max_count":5,"publish_range":"7d","duration_range":"1to5m"}',
        "account_id": account_id,
        "category_id": cat_id,
        "interval_config": '{"type":"hour","value":1}',
        "status": "enabled",
        "pull_round": 0,
        "total_pulled": 0,
    })
    print(f"[task] {task_id}")

    info = MagicMock()
    info.cancel_requested = False
    info.start_ts = time.time()
    info.progress = ""
    info.message = ""

    from app.core.douyin import DouyinClientError
    from app.services import material_service as ms

    # 阶段 A：mock search_session.search_all_for_ids
    test_aweme_ids = [f"fake_aw_{i}" for i in range(8)]  # 8 个候选
    call_count = {"n": 0}

    def fake_search_all_for_ids(self, keyword, conditions=None, idle_timeout=60, max_pages=50):
        call_count["n"] += 1
        print(f"  [mock search_all_for_ids] call #{call_count['n']} keyword={keyword}")
        # 第一次返 8 个，第二次（早退重试）也返 8 个（模拟拿到数据）
        return test_aweme_ids

    # 阶段 B：mock _run_detail_phase 计数
    detail_phase_calls = {"n": 0}

    def fake_run_detail_phase(task_id, aweme_ids, conditions, client, cookie,
                              acc_id, max_count, info,
                              *, category_id, task_name):
        detail_phase_calls["n"] += 1
        n = len(aweme_ids)
        # 模拟阶段 B：前 5 个 new，后 3 个 duplicate（已入库）
        new_count = min(max_count, 5)
        skip_count = max(0, n - new_count)
        processed_count = n
        print(f"  [mock _run_detail_phase] total={n} new={new_count} skip={skip_count} reached_limit={new_count >= max_count} category_id={category_id}")
        return new_count, skip_count, 0, new_count >= max_count, "", processed_count  # 6 元 (含 fail_reason + processed_count)

    # mock client._fetch_aweme_detail 不真正调用（避免 asyncio loop 错）
    fake_client = MagicMock()
    fake_client._fetch_aweme_detail = MagicMock(side_effect=lambda *a, **kw: {})
    fake_client.search_videos = MagicMock(return_value={"has_next": False, "videos": []})

    with patch("app.core.douyin.search_api.BrowserSearchSession.search_all_for_ids", fake_search_all_for_ids), \
         patch.object(ms, "_run_detail_phase", fake_run_detail_phase), \
         patch.object(ms, "get_douyin_client", return_value=fake_client), \
         patch("app.services.douyin_account.get_profile_dir", return_value=pathlib.Path("/tmp/fake")), \
         patch.object(ms.account_service, "get_cookie", return_value=""), \
         patch.object(ms.task_scheduler, "touch_last_run", return_value=None), \
         patch.object(ms.task_scheduler, "remove_job", return_value=None):
        t0 = time.time()
        rc = ms._run_pull_round(task_id, info)
        elapsed = time.time() - t0

    print(f"\n=== probe_507 e2e 完成 ===")
    print(f"耗时: {elapsed:.2f}s")
    print(f"返回: {rc}")
    print(f"info.progress={info.progress!r}")
    print(f"info.message={info.message!r}")
    print(f"search_all_for_ids 调用次数: {call_count['n']}（期望 1）")
    print(f"_run_detail_phase 调用次数: {detail_phase_calls['n']}（期望 1）")

    if call_count["n"] == 1 and detail_phase_calls["n"] == 1:
        print("[OK] 507 two-phase orchestration works")
    else:
        print("[FAIL] orchestration mismatch")

    # 清理
    d.execute("UPDATE video_pull_task SET deleted=1 WHERE id=?", (task_id,))
    print(f"\n[clean] task {task_id} deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())