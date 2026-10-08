# -*- coding: utf-8 -*-
"""#597 发布管理时间窗校验：起始时间不能是过去（未来才允许）。

覆盖：
- _validate_payload 拒绝 start_time <= now
- _soft_validate_payload 同样拒绝
- 合法未来时间 + 同日 + end > start 通过
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from app.db import database as db_module
from app.db import migrations
from app.services import publish_service


@pytest.fixture(autouse=True)
def _init_db() -> None:
    """每个测试一个临时 DB（含 v1 + 后续迁移 + 1 个账号/项目/门店）。"""
    tmp = Path(tempfile.mkdtemp(prefix="dt_597_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    db_path = tmp / "db" / "short_video_tools.db"
    db_module.init_db(db_path)
    migrations.migrate(db_module.db)
    # 注入 1 个账号 + 1 个项目 + 1 个店 + 1 个简介（绕开业务硬校验）
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
        "id": "shopA", "poi_id": "poi_a", "name": "店A",
        "is_added_to_library": 1, "is_cps": 1,
        "create_time": "2026-09-22 00:00:00", "update_time": "2026-09-22 00:00:00",
    })
    db_module.db.insert("project_shop", {
        "project_id": "projA", "shop_id": "shopA", "sort_order": 0,
    })
    yield


def _base_payload(start_time: str, end_time: str) -> dict:
    """最小合法 payload（已注入账号/项目/门店，业务硬校验不抛）。"""
    return {
        "task_name": "t1",
        "account_ids": ["accA"],
        "daily_limit_mode": "global",
        "daily_limit_global": 10,
        "daily_limit_per_account": [],
        "projects": [{
            "project_id": "projA",
            "shop_ids": ["shopA"],
            "intro_ids": [],
        }],
        "start_time": start_time,
        "end_time": end_time,
        "declaration": "无需添加自主声明",
        "allow_download": False,
    }


def test_validate_payload_rejects_past_start() -> None:
    """_validate_payload：起始时间 ≤ now 抛 ValueError。"""
    past_start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    future_end = (datetime.now() + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    payload = _base_payload(past_start, future_end)
    with pytest.raises(ValueError, match=r"起始时间必须晚于当前"):
        publish_service._validate_payload(payload)


def test_soft_validate_payload_rejects_past_start() -> None:
    """_soft_validate_payload：同样拦截过去起始时间。"""
    past_start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    future_end = (datetime.now() + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    payload = _base_payload(past_start, future_end)
    with pytest.raises(ValueError, match=r"起始时间必须晚于当前"):
        publish_service._soft_validate_payload(payload)


def test_validate_payload_accepts_future_start() -> None:
    """_validate_payload：起始为未来时间（明天 07:00）+ 同日 + end > start 通过。"""
    start = datetime.now() + timedelta(days=1)
    start = start.replace(hour=7, minute=0, second=0, microsecond=0)
    end = start.replace(hour=22, minute=0)
    payload = _base_payload(
        start.strftime("%Y-%m-%d %H:%M"),
        end.strftime("%Y-%m-%d %H:%M"),
    )
    publish_service._validate_payload(payload)


def test_validate_payload_rejects_now_exact() -> None:
    """边界：start = now（精确等于）应被拒绝（不允许等于，条件是 <=）。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    future_end = (datetime.now() + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    payload = _base_payload(now_str, future_end)
    with pytest.raises(ValueError, match=r"起始时间必须晚于当前"):
        publish_service._validate_payload(payload)