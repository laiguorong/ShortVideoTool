# -*- coding: utf-8 -*-
"""数据库工具：UUID 生成、时间格式化、通用字段填充。

约定（见需求文档 6.3 通用字段约定）：
- 主键：TEXT 存 UUID v4 去连字符 32 位小写十六进制，应用层生成，禁止自增整型；
- 通用字段：id / create_time / update_time / deleted；
- 时间格式：yyyy-MM-dd HH:mm:ss（本地时区）。
"""

import uuid
from datetime import datetime


def new_id() -> str:
    """生成 32 位小写十六进制 UUID 主键。"""
    return uuid.uuid4().hex


def now_str() -> str:
    """当前本地时间字符串，格式 yyyy-MM-dd HH:mm:ss。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    """当前本地日期字符串，格式 yyyy-MM-dd（日快照 stat_date 用）。"""
    return datetime.now().strftime("%Y-%m-%d")


def fill_common_fields(record: dict, is_insert: bool = True) -> dict:
    """为记录补充通用字段。

    参数:
        record: 业务字段字典（不含通用字段）
        is_insert: True 表示插入（补 id/create_time/update_time/deleted），False 表示更新（仅刷新 update_time）
    返回:
        补全后的字典（原字典被就地修改并返回）
    """
    if is_insert:
        record.setdefault("id", new_id())
        record.setdefault("create_time", now_str())
        record.setdefault("deleted", 0)
    record["update_time"] = now_str()
    return record
