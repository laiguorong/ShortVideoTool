# -*- coding: utf-8 -*-
"""API 路由聚合：各模块子路由统一挂到 api_router（main.py 以 /api 前缀 include）。"""

from fastapi import APIRouter

from app.api import accounts, creation, dashboard, files, materials, publish, publish_intro, selection, settings, startup, tasks

api_router = APIRouter()
api_router.include_router(accounts.router)
api_router.include_router(dashboard.router)
api_router.include_router(selection.router)
api_router.include_router(materials.router)
api_router.include_router(creation.router)
api_router.include_router(publish.router)
api_router.include_router(publish_intro.router)
api_router.include_router(settings.router)
api_router.include_router(tasks.router)
api_router.include_router(files.router)
api_router.include_router(startup.router)




