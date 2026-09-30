# -*- coding: utf-8 -*-
"""probe 验证 material_service 单 session 复用（mocked 隔离层）。

跳过真实浏览器/cookie/账号管理器，用 mock 隔离：
- client.search_videos：每次返 5 条固定 video
- client._fetch_aweme_detail：返 _source='detail'，下游 _download_and_ingest 短路
- task_scheduler.touch_last_run：no-op，避免被 interval_config 校验打断

验证点：
1. BrowserSearchSession 整轮仅构造 1 次（_session_id 不变）
2. 跨页累积 page=1/2/3/4 同 session 复用
3. 早退重试路径 page=4 同样复用 session（无重建）
4. _process_single_video 通过 detail 短路进入 download，download 抛 DouyinClientError
   计入 skip_count 'failed'（mock 设计），new_count=0 触发早退重试
5. 总耗时 ~75s（含 30+45s 重试 sleep，主循环 mock 即时返回）

#109/#110/#111 修复：
- mock _fetch_aweme_detail 返 _source='detail'，避免 _download_and_ingest 计入 'failed'
  污染 skip_count（之前 mock 缺 _source，下游误以为搜索侧 video）
- mock task_scheduler.touch_last_run，与 client 一致隔离
- 封装 make_mock_info helper 处理 MagicMock __bool__ 坑
"""
import sys
import time
import pathlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def make_fake_video(i: int, page: int, *, source: str = "search") -> dict:
    """生成统一视频字段 dict（_process_single_video 期望结构）。

    参数:
        source: 'search'（搜索侧，触发补抓 detail）/ 'detail'（detail 侧，短路补抓）
    """
    v = {
        "video_id": f"fake_v_{page}_{i}",
        "title": f"测试视频 page{page} 第{i+1}条",
        "desc": "probe fake desc",
        "duration_ms": 30000,
        "publish_time": "2026-09-01 12:00:00",
        "width": 1080,
        "height": 1920,
        "orientation": "vertical",
        "share_url": f"https://www.douyin.com/video/fake_v_{page}_{i}",
        "download_url": "",  # 空 → _download_with_retry 抛 DouyinClientError（mock 预期）
        "cover_url": "",
        "author_avatar": "",
        "author_nickname": f"作者{page}-{i}",
        "digg_count": 100, "comment_count": 10, "collect_count": 5, "share_count": 1,
    }
    if source:
        v["_source"] = source
    return v


def make_mock_info() -> MagicMock:
    """构造 _run_pull_round 期望的 info mock。

    MagicMock 默认 __bool__ True → raise_for_cancel(info) 立即抛 _TaskCancelled
    （因 info.cancel_requested 真值测试通过）。显式 cancel_requested=False 规避。
    """
    info = MagicMock()
    info.cancel_requested = False
    info.start_ts = time.time()
    info.progress = ""
    info.message = ""
    return info


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
        print("[FAIL] 无可用账号")
        return 1
    account_id = row["id"]
    print(f"[账号] {account_id} {row['nickname']}")

    cat = d.query_one(
        "SELECT id FROM material_category WHERE deleted=0 AND type='video' LIMIT 1"
    )
    cat_id = cat["id"] if cat else d.insert("material_category", {
        "name": "probe_mock", "type": "video", "parent_id": "", "sort_order": 99})
    print(f"[分类] {cat_id}")

    # 显式 pull_round=0/total_pulled=0，避免依赖 DB 默认 NULL → 0 容错
    task_id = d.insert("video_pull_task", {
        "task_name": "probe_v5_mocked",
        "conditions_json": '{"keyword":"test_probe","max_count":15}',
        "account_id": account_id,
        "category_id": cat_id,
        "interval_config": '{"type":"hour","value":1}',
        "status": "enabled",
        "pull_round": 0,
        "total_pulled": 0,
    })
    print(f"[任务] {task_id}")

    # Mock client.search_videos：每次返回 5 条 + has_next
    call_count = {"n": 0}
    page_state = {"current": 1}
    session_ids: list[int] = []

    def fake_search_videos(profile_dir, conditions, page, search_id="", _session=None):
        call_count["n"] += 1
        page_state["current"] = page
        sid = id(_session) if _session else None
        session_ids.append(sid)
        print(f"  [mock search_videos] 第 {call_count['n']} 次 page={page} _session_id={sid}")
        videos = [make_fake_video(i, page, source="search") for i in range(5)]
        # 第 1 轮 4 页后返回 has_more=False（早退重试触发）
        has_next = call_count["n"] < 4
        return {"has_next": has_next, "videos": videos}

    info = make_mock_info()

    # mock client
    from app.core.douyin import DouyinClientError
    fake_client = MagicMock()
    fake_client.search_videos = fake_search_videos
    # #109：mock _fetch_aweme_detail 返 _source='detail'，让 _process_single_video
    # 第 1578 行 `source == 'pull' and video.get('_source') != 'detail'` 短路，
    # 跳过二次补抓直接进入 _download_and_ingest。download 抛 DouyinClientError
    # 计入 skip_count 'failed'（probe 预期行为）。
    fake_client._fetch_aweme_detail = MagicMock(
        side_effect=lambda *a, **kw: make_fake_video(0, 0, source="detail"))
    fake_client.download_video = MagicMock(side_effect=DouyinClientError("mock skip download"))

    # #110：mock task_scheduler + get_cookie 隔离，与 client 一致
    # 空 cookie_encrypted 时 get_cookie 内部 crypto.decrypt('') 抛 InvalidToken，
    # 直接 mock 返回空字符串绕过
    from app.services import material_service as ms
    import app.core.douyin as dc_module
    with patch.object(dc_module, "get_douyin_client", return_value=fake_client), \
         patch.object(ms, "get_douyin_client", return_value=fake_client), \
         patch.object(ms, "account_service", MagicMock(get_cookie=lambda aid: "")), \
         patch("app.services.douyin_account.get_profile_dir", return_value=pathlib.Path("/tmp/fake_profile")), \
         patch.object(ms.task_scheduler, "touch_last_run", return_value=None), \
         patch.object(ms.task_scheduler, "remove_job", return_value=None):
        t0 = time.time()
        rc = ms._run_pull_round(task_id, info)
        elapsed = time.time() - t0

    print(f"\n=== probe 完成 ===")
    print(f"耗时: {elapsed:.2f}s")
    print(f"返回: {rc}")
    print(f"info.progress={info.progress!r}")
    print(f"info.message={info.message!r}")
    print(f"search_videos 调用次数: {call_count['n']}")
    print(f"最终 page: {page_state['current']}")

    # 单 session 验证：所有 _session_id 一致
    unique_sids = set(session_ids)
    print(f"唯一 _session_id 数: {len(unique_sids)}（期望 1，整轮单 session 复用）")
    if len(unique_sids) == 1 and None not in unique_sids:
        print("[OK] single session reused")
    elif None in unique_sids:
        print("[FAIL] contains None session_id (_session not passed)")
    else:
        print(f"[FAIL] multiple sessions: {unique_sids}")

    # 清理
    d.execute("UPDATE video_pull_task SET deleted=1 WHERE id=?", (task_id,))
    print(f"\n[清理] 任务 {task_id} 已软删")
    return 0


if __name__ == "__main__":
    sys.exit(main())