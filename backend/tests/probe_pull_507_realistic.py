# -*- coding: utf-8 -*-
"""507 真机 probe：跑完整 _run_pull_round（阶段 A 真搜索 + 阶段 B 真详情+下载）。

不 mock search_session、不 mock _fetch_aweme_detail——
走 production 路径，验证：
1. 阶段 A 浏览器持久化搜索（filter panel UI）
2. 阶段 B 复用阶段 A page 抓详情（page 复用方案）
3. 阶段 B 顺序处理：详情抓取 + 下载 + 入库

需要：
- 至少 1 个 status='normal' 账号（持久化 chromium profile 已登录）
- frontend 配置 browser_show_window=true（看得到浏览器更直观）
"""
import sys
import time
import pathlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main() -> int:
    from app.services.setting_service import init_data_dir, get_data_dir, get_config_dir, save_settings
    from app.core import crypto
    from app.db.database import init_db, get_db
    init_data_dir()
    crypto.init_crypto(get_config_dir())
    data_dir = get_data_dir()
    init_db(data_dir / "shortvideotool.db")
    # probe 模式：开启慢路径（每步 2.5s）让用户看清筛选操作
    save_settings({"pull_debug": True})
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
        "name": "probe_507_real", "type": "video", "parent_id": "", "sort_order": 99})
    print(f"[分类] {cat_id}")

    task_id = d.insert("video_pull_task", {
        "task_name": "probe_507_realistic",
        # 完整 4 项筛选：排序=最新发布 / 发布时间=一周内 / 视频时长=1分钟以下 / 内容形式=视频
        "conditions_json": '{"keyword":"鱼公元鱼生","max_count":2,"publish_range":"7d","duration_range":"lt1m"}',
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
    from app.core.douyin.search_api import BrowserSearchSession

    # 真机观察：material_service 写死 headless=True。probe 临时 monkey-patch
    # __init__ 强制 headless=False，让浏览器窗口弹出来便于观察 UI 操作问题。
    _orig_init = BrowserSearchSession.__init__

    def _patched_init(self, profile_dir, headless=True):
        _orig_init(self, profile_dir, headless=False)

    # 只隔离 scheduler 副作用（不真正注册到 APScheduler）
    with patch.object(BrowserSearchSession, "__init__", _patched_init), \
         patch.object(ms.task_scheduler, "touch_last_run", return_value=None), \
         patch.object(ms.task_scheduler, "remove_job", return_value=None):
        t0 = time.time()
        rc = ms._run_pull_round(task_id, info)
        elapsed = time.time() - t0

    print(f"\n=== probe_507 真机完成 ===")
    print(f"耗时: {elapsed:.2f}s")
    print(f"返回: {rc}")
    print(f"info.progress={info.progress!r}")
    print(f"info.message={info.message!r}")

    # 清理
    d.execute("UPDATE video_pull_task SET deleted=1 WHERE id=?", (task_id,))
    print(f"[清理] 任务 {task_id} 已软删")
    return 0


if __name__ == "__main__":
    sys.exit(main())