# -*- coding: utf-8 -*-
"""数据库层包：导出单例与初始化入口。"""

from app.db.database import Database, db, get_db, init_db
from app.db.utils import new_id, now_str, today_str, fill_common_fields

__all__ = ["Database", "db", "get_db", "init_db", "new_id", "now_str", "today_str", "fill_common_fields"]
