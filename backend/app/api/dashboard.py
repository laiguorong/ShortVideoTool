# -*- coding: utf-8 -*-
"""工作台路由（9.1.2）：首页指标卡 + 待处理事项聚合。

#500 数据中心功能移除后，/dashboard 从原 stats 路由迁出独立挂载，
返回结构去掉 today（今日播放/GMV/佣金）速览。
"""

from fastapi import APIRouter

from app.services import dashboard_service

router = APIRouter(tags=["工作台"])


@router.get("/dashboard", summary="工作台聚合")
def dashboard():
    """指标卡 + 待处理事项。"""
    return dashboard_service.dashboard_summary()
