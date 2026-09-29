# -*- coding: utf-8 -*-
"""工作台聚合服务（9.1.2）：首页指标卡 + 待处理事项。

#500 数据中心功能移除后，本服务只保留不依赖视频/带货日快照表的聚合：
- 指标卡：账号数/失效数、今日新增门店、今日新增素材、可用/占用成品
- 待处理：失效账号、进行中发布任务

原"今日播放/GMV/结算佣金"速览依赖 video_stats_daily / sales_stats_daily，
随数据中心功能一并删除（表已 DROP）。
"""

from app.db import get_db
from app.db.utils import today_str


def dashboard_summary() -> dict:
    """工作台指标卡 + 待处理事项。

    返回结构:
        {
          "cards": {账号数/失效数/今日新增门店/今日新增素材/可用成品/占用成品},
          "todo": {失效账号列表/进行中发布任务列表}
        }
    """
    d = get_db()
    today = today_str()
    # 账号总数 + 失效数
    accounts = d.query_one(
        "SELECT COUNT(*) AS c, "
        "SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END) AS invalid "
        "FROM account WHERE deleted=0")
    # 门店总数 + 今日新增（v33 后任务-门店解耦，今日新增按 update_time 计）
    shops_total = d.query_one("SELECT COUNT(*) AS c FROM shop WHERE deleted=0")
    shops_today = d.query_one(
        "SELECT COUNT(*) AS c FROM shop WHERE deleted=0 AND update_time LIKE ?",
        (f"{today}%",))
    # 素材总数 + 今日新增
    materials_total = d.query_one("SELECT COUNT(*) AS c FROM material WHERE deleted=0")
    materials_today = d.query_one(
        "SELECT COUNT(*) AS c FROM material WHERE deleted=0 AND create_time LIKE ?",
        (f"{today}%",))
    # 成品可用/占用
    idle_videos = d.query_one(
        "SELECT COUNT(*) AS c FROM generated_video WHERE status='idle'")
    occupied_videos = d.query_one(
        "SELECT COUNT(*) AS c FROM generated_video WHERE status='occupied'")
    # 待处理：失效账号（最多 5 条）
    todo_accounts = d.query_all(
        "SELECT id, remark, nickname FROM account "
        "WHERE deleted=0 AND status='invalid' LIMIT 5")
    # 待处理：进行中发布任务（最多 5 条）
    running_tasks = d.query_all(
        "SELECT id, task_name, start_time FROM publish_task "
        "WHERE status='running' LIMIT 5")
    return {
        "cards": {
            "account_count": accounts["c"],
            "account_invalid": accounts["invalid"] or 0,
            "shop_total": shops_total["c"],
            "shop_new_today": shops_today["c"],
            "material_total": materials_total["c"],
            "material_new_today": materials_today["c"],
            "video_idle": idle_videos["c"],
            "video_occupied": occupied_videos["c"],
        },
        "todo": {
            "invalid_accounts": todo_accounts,
            "running_publish_tasks": running_tasks,
        },
    }
