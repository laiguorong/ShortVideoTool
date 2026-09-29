# -*- coding: utf-8 -*-
"""选品中心路由（重写 #449）。

路由表：
- POST /selection/tasks              创建任务
- GET  /selection/tasks              任务列表
- GET  /selection/tasks/{id}         任务详情
- PATCH /selection/tasks/{id}        编辑任务
- DELETE /selection/tasks/{id}       删除任务
- POST /selection/tasks/{id}/toggle  启停任务
- POST /selection/tasks/{id}/run     手动触发一轮
- GET  /selection/tasks/{id}/logs    执行日志
- GET  /selection/shops              门店查询（多维筛选）
- GET  /selection/cities             已入库城市列表
- GET  /selection/provinces          已入库省份列表
- GET  /selection/categories         已入库分类列表
"""

from fastapi import APIRouter, HTTPException, Query

from app.models.selection import (
    CreateShopPullTaskRequest,
    ToggleRequest,
    UpdateShopPullTaskRequest,
)
from app.services import selection_service

router = APIRouter(prefix="/selection", tags=["选品中心"])


# ============ 任务管理 ============

@router.post("/tasks", summary="创建选品拉取任务")
def create_task(body: CreateShopPullTaskRequest):
    """创建选品拉取任务，立即注册到调度器（status=enabled）。"""
    try:
        task = selection_service.create_task(
            task_name=body.task_name,
            keyword=body.keyword,
            cities=body.cities,
            account_id=body.account_id,
            interval=body.interval.model_dump(),
            shop_name_pattern=body.shop_name_pattern,
            city_pattern=body.city_pattern,
        )
    except ValueError as e:
        raise HTTPException(400, f"任务参数非法：{e}")
    return task


@router.get("/tasks", summary="任务列表")
def list_tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """任务列表（按 create_time 倒序分页）。"""
    return selection_service.list_tasks(page=page, page_size=page_size)


@router.get("/tasks/{task_id}", summary="任务详情")
def get_task(task_id: str):
    """读取单个任务。"""
    task = selection_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.patch("/tasks/{task_id}", summary="编辑任务")
def update_task(task_id: str, body: UpdateShopPullTaskRequest):
    """编辑任务（仅更新传入项，重置调度）。"""
    update_dict = body.model_dump(exclude_unset=True)
    task = selection_service.update_task(task_id, **update_dict)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.delete("/tasks/{task_id}", summary="删除任务")
def delete_task(task_id: str):
    """软删任务并移除调度。"""
    ok = selection_service.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@router.post("/tasks/{task_id}/toggle", summary="启停任务")
def toggle_task(task_id: str, body: ToggleRequest):
    """启停任务。"""
    task = selection_service.toggle_task(task_id, enabled=body.enabled)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.post("/tasks/{task_id}/run", summary="手动触发一轮")
def run_task(task_id: str):
    """投递到 task_service 队列，立即返回 bg_task_id。"""
    task = selection_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    bg_id = selection_service.run_task_now(task_id)
    return {"ok": True, "bg_task_id": bg_id}


@router.get("/tasks/{task_id}/logs", summary="执行日志")
def list_task_logs(task_id: str,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(50, ge=1, le=200)):
    """任务执行日志（按 run_time 倒序分页）。"""
    return selection_service.list_logs(task_id, page=page, page_size=page_size)


@router.get("/logs", summary="全部门店拉取记录")
def list_all_shop_logs(page: int = Query(1, ge=1),
                       page_size: int = Query(50, ge=1, le=200)):
    """全部门店拉取执行记录（按 run_time 倒序），JOIN 任务名。"""
    return selection_service.list_all_logs(page=page, page_size=page_size)


@router.post("/logs/batch-delete", summary="批量删除门店拉取记录")
def batch_delete_logs(body: dict):
    """批量硬删门店拉取执行记录。

    body: {"ids": [log_id, ...]}
    """
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须为非空数组")
    return selection_service.delete_logs([str(i) for i in ids])


# ============ 门店查询 ============

@router.get("/shops", summary="门店分页列表")
def list_shops(
    keyword: str = Query("", description="门店名模糊"),
    category: str = Query("", description="一级分类精确"),
    city: str = Query("", description="城市精确"),
    province: str = Query("", description="省份精确"),
    # 三态可选：None=全部 / True=仅 true / False=仅 false
    # 之前 bool=False 时，"仅无佣金/仅未加库"无法表达，会回退到"全部"
    is_cps_only: bool | None = Query(None, description="None=全部 / True=仅 CPS / False=仅非 CPS"),
    is_added_only: bool | None = Query(None, description="None=全部 / True=仅已加库 / False=仅未加库"),
    spu_count_min: int | None = Query(None, description="商品数下限"),
    spu_count_max: int | None = Query(None, description="商品数上限"),
    total_gmv_min: float | None = Query(None, description="总 GMV 下限"),
    total_gmv_max: float | None = Query(None, description="总 GMV 上限"),
    take_rate_min: int | None = Query(None, description="佣金率下限（万分之）"),
    take_rate_max: int | None = Query(None, description="佣金率上限（万分之）"),
    commission_min: float | None = Query(None, description="佣金率下限（%）"),
    commission_max: float | None = Query(None, description="佣金率上限（%）"),
    sort: str = Query("total_gmv", description="排序字段"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """门店分页列表（多维筛选）。"""
    return selection_service.list_shops(
        keyword=keyword or None,
        category=category or None,
        city=city or None,
        province=province or None,
        is_cps_only=is_cps_only,
        is_added_only=is_added_only,
        spu_count_min=spu_count_min,
        spu_count_max=spu_count_max,
        total_gmv_min=total_gmv_min,
        total_gmv_max=total_gmv_max,
        take_rate_min=take_rate_min,
        take_rate_max=take_rate_max,
        commission_min=commission_min,
        commission_max=commission_max,
        sort=sort,
        page=page,
        page_size=page_size,
    )


# ============ 维度聚合（下拉数据源）============

@router.get("/cities", summary="已入库城市列表")
def get_cities():
    """已入库城市（distinct，按字母升序）。"""
    return {"cities": selection_service.list_cities()}


@router.get("/provinces", summary="已入库省份列表")
def get_provinces():
    """已入库省份（distinct）。"""
    return {"provinces": selection_service.list_provinces()}


@router.get("/categories", summary="已入库分类列表")
def get_categories():
    """已入库一级分类（distinct）。"""
    return {"categories": selection_service.list_categories()}


@router.post("/shops/{shop_id}/toggle-added", summary="切换门店加库标记")
def toggle_shop_added(shop_id: str, enabled: bool = Query(True, description="True=加库 / False=取消")):
    """切换门店「已加库」标记。"""
    try:
        return selection_service.set_shop_added(shop_id, enabled)
    except ValueError as e:
        raise HTTPException(404, str(e))