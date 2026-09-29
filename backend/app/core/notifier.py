# -*- coding: utf-8 -*-
"""通知中心（需求文档 3.3-6）。

事件产生 → 入通知中心（未读）→ 用户查看（置已读）→ 执行对应动作 → 标记已处理；
通知落 notification 表，保留 30 天自动清理；重要通知（登录态失效、任务自动停用）level=warn/error。
"""

from datetime import datetime, timedelta

from app.db import get_db
from app.db.utils import new_id, now_str


def notify(level: str, category: str, title: str, content: str = "", action: str = "") -> str:
    """发送一条通知。

    参数:
        level: info / warn / error
        category: account / task / publish / system
        title: 通知标题
        content: 通知正文
        action: 处理动作标识（如 "relogin:{账号ID}"，前端据此渲染一键处理按钮）
    返回:
        通知 ID
    """
    d = get_db()
    record = {
        "id": new_id(),
        "level": level,
        "category": category,
        "title": title,
        "content": content,
        "is_read": 0,
        "handled": 0,
        "action": action,
        "create_time": now_str(),
    }
    d.insert("notification", record)
    return record["id"]


def list_notifications(only_unread: bool = False, page: int = 1, page_size: int = 50) -> dict:
    """分页查询通知列表（时间倒序）。

    参数:
        only_unread: 仅未读
        page / page_size: 分页参数
    返回:
        分页结果 dict（含 unread_count 未读总数）
    """
    d = get_db()
    where = "WHERE is_read=0" if only_unread else ""
    result = d.query_page(f"SELECT * FROM notification {where} ORDER BY create_time DESC", (), page, page_size)
    unread = d.query_one("SELECT COUNT(*) AS c FROM notification WHERE is_read=0")
    result["unread_count"] = unread["c"] if unread else 0
    return result


def mark_read(notification_id: str = "", all_read: bool = False) -> int:
    """标记已读（单条或全部）。"""
    d = get_db()
    if all_read:
        return d.execute("UPDATE notification SET is_read=1")
    return d.execute("UPDATE notification SET is_read=1 WHERE id=?", (notification_id,))


def mark_handled(notification_id: str) -> int:
    """执行动作后标记已处理。"""
    d = get_db()
    return d.execute("UPDATE notification SET handled=1 WHERE id=?", (notification_id,))


def cleanup_expired(retention_days: int = 30) -> int:
    """清理超期通知（默认 30 天），返回清理条数。"""
    d = get_db()
    deadline = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    return d.execute("DELETE FROM notification WHERE create_time < ?", (deadline,))
