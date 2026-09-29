# -*- coding: utf-8 -*-
"""通用后台任务路由：任务列表 / 概览 / 取消（底部状态栏数据源）。"""

from fastapi import APIRouter, HTTPException

from app.services.task_service import task_service

router = APIRouter(prefix="/tasks", tags=["后台任务"])


@router.get("", summary="后台任务列表")
def list_tasks(running_only: bool = False):
    """任务列表（时间倒序）。"""
    return task_service.list_tasks(running_only)


@router.get("/summary", summary="任务概览（状态栏）")
def summary():
    """各类型执行中数量。"""
    return task_service.summary()


@router.get("/{task_id}", summary="任务详情")
def get_task(task_id: str):
    """查询单个任务。"""
    info = task_service.get_task(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在")
    return info


@router.post("/{task_id}/cancel", summary="取消任务")
def cancel_task(task_id: str):
    """请求取消（worker 自查标记后停止）。"""
    ok = task_service.request_cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已结束")
    return {"ok": True}
