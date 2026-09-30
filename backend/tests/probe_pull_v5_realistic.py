# -*- coding: utf-8 -*-
"""⚠️ 路径已过时（507 改造后）。当前 production 路径走
probe_pull_507_e2e.py —— 阶段 A 单次 search_all_for_ids + 阶段 B 独立 BrowserActor。
本 probe 仅保留作为 v5 wheel 翻页的诊断工具（看 XHR URL / 每页耗时），
实测 production 路径不再调 search_videos 多次。如需回归请跑 507 e2e。

----

probe 真实跑 v5 wheel 整轮，记录每页耗时 + 所有 XHR URL。

调试用：看为什么某些情况下 general/search/single 不触发。
"""
import sys
import time
import pathlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main() -> int:
    from app.services.setting_service import init_data_dir, get_data_dir, get_config_dir
    from app.core import crypto, douyin as dc_module
    from app.core.douyin import search_api as sa
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
        "name": "probe_v5_real", "type": "video", "parent_id": "", "sort_order": 99})
    print(f"[分类] {cat_id}")

    fake_cookie = "sessionid=fake_probe_sessionid_for_v5_real"
    d.update_by_id("account", account_id, {"cookie_encrypted": crypto.encrypt(fake_cookie)})

    task_id = d.insert("video_pull_task", {
        "task_name": "probe_v5_realistic",
        "conditions_json": '{"keyword":"鱼公元鱼生","max_count":30}',
        "account_id": account_id,
        "category_id": cat_id,
        "interval_config": '{"type":"hour","value":1}',
        "status": "enabled",
        "pull_round": 0,
        "total_pulled": 0,
    })
    print(f"[任务] {task_id}")

    info = MagicMock()
    info.cancel_requested = False
    info.start_ts = time.time()
    info.progress = ""
    info.message = ""

    from app.services import material_service as ms

    # XHR 日志 + search_videos 计时
    page_times = []
    all_xhr: list[str] = []

    def log_resp(r):
        url = r.url
        if "aweme/v1/web" in url:
            all_xhr.append(f"  [XHR] {url[:180]}")

    def timed_search_videos(*args, **kwargs):
        t0 = time.time()
        result = ms._orig_search_videos(*args, **kwargs)
        elapsed = time.time() - t0
        page = args[2] if len(args) > 2 else kwargs.get('page', '?')
        sid = id(args[4]) if len(args) > 4 and args[4] else (id(kwargs.get('_session')) if kwargs.get('_session') else None)
        page_times.append((page, elapsed, sid, len(result.get('videos', []))))
        return result

    # 包装 _ensure_open 让 page.on 注册
    orig_ensure_open = sa.BrowserSearchSession._ensure_open
    def patched_ensure_open(self):
        orig_ensure_open(self)
        try:
            self._page.on("response", log_resp)
        except Exception:
            pass

    # 保存原 search_videos 用于 timing 包装
    ms._orig_search_videos = dc_module.DouyinClient.search_videos

    with patch.object(dc_module.DouyinClient, "search_videos", timed_search_videos), \
         patch.object(sa.BrowserSearchSession, "_ensure_open", patched_ensure_open), \
         patch.object(ms.task_scheduler, "touch_last_run", return_value=None), \
         patch.object(ms.task_scheduler, "remove_job", return_value=None):
        t0 = time.time()
        rc = ms._run_pull_round(task_id, info)
        total_elapsed = time.time() - t0

    print(f"\n=== probe_v5 真实整轮 ===")
    print(f"总耗时: {total_elapsed:.2f}s")
    print(f"返回: {rc}")
    print(f"info.progress={info.progress!r}")
    print(f"info.message={info.message!r}")

    print(f"\n所有 aweme/v1/web XHR:")
    for x in all_xhr:
        print(x)

    print(f"\n每页 search_videos 耗时:")
    for i, (page, t, sid, n) in enumerate(page_times, 1):
        print(f"  [{i:02d}] page={page} search={t:.2f}s _session_id={sid} videos={n}")

    unique_sids = set(s for _, _, s, _ in page_times)
    print(f"\n唯一 _session_id 数: {len(unique_sids)}")
    if len(unique_sids) == 1 and None not in unique_sids:
        print("[OK] single session reused (v5)")
    else:
        print(f"[INFO] sids={unique_sids}")

    d.execute("UPDATE video_pull_task SET deleted=1 WHERE id=?", (task_id,))
    d.update_by_id("account", account_id, {"cookie_encrypted": ""})
    print(f"\n[清理] 任务 {task_id} 已软删")
    return 0


if __name__ == "__main__":
    sys.exit(main())