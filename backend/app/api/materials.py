# -*- coding: utf-8 -*-
"""素材库路由（F-03）。"""

from fastapi import APIRouter, HTTPException

from app.models.selection import (CreateCategoryRequest, CreateVideoPullTaskRequest,
                                  DeleteCategoryRequest, DeleteMaterialRequest,
                                  ImportShareRequest, MoveCategoryRequest,
                                  RetryShareRequest,
                                  RelocateMaterialRequest, RenameCategoryRequest,
                                  ToggleRequest, UpdateMaterialRequest,
                                  UpdateVideoPullTaskRequest, UploadFilesRequest)
from app.services import material_service
from app.services.share_import_service import (
    create_task as create_share_import_task,
    delete_task as delete_share_import_task,
    get_task as get_share_import_task,
    list_items as list_share_import_items,
    list_tasks as list_share_import_tasks,
    retry_failed as retry_share_import_task,
    retry_item as retry_share_import_item,
)
from app.services.upload_service import (
    create_task as create_upload_task,
    delete_task as _delete_upload_task,
    get_task as _get_upload_task,
    list_items as _list_upload_items,
    list_tasks as list_upload_tasks,
    retry_failed as _retry_upload_task,
    retry_item as _retry_upload_item,
)

router = APIRouter(prefix="/materials", tags=["素材库"])


# ---------- 分类树 ----------

@router.get("/categories", summary="分类树列表")
def list_categories(type: str = "video", orientation: str = ""):
    """分类树（平铺含 count，前端组树）。

    #441：orientation 非空时按 m.orientation 过滤每个分类的素材计数（与 list_materials 一致）。
    """
    return material_service.list_categories(type, orientation=orientation)


@router.post("/categories", summary="新增分类")
def create_category(req: CreateCategoryRequest):
    """新增子分类（层级上限 4）。"""
    try:
        return material_service.create_category(req.name, req.type, req.parent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/categories/{category_id}/rename", summary="重命名分类")
def rename_category(category_id: str, req: RenameCategoryRequest):
    """重命名。"""
    material_service.rename_category(category_id, req.name)
    return {"ok": True}


@router.post("/categories/{category_id}/move", summary="移动分类")
def move_category(category_id: str, req: MoveCategoryRequest):
    """换父级/排序。"""
    try:
        material_service.move_category(category_id, req.new_parent_id, req.sort_order)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/categories/{category_id}", summary="删除分类")
def delete_category(category_id: str, strategy: str = "to_parent"):
    """删除分类（无子分类；素材按策略处置）。"""
    try:
        material_service.delete_category(category_id, strategy)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- 视频拉取任务 ----------

@router.post("/pull-tasks", summary="创建视频拉取任务")
def create_pull_task(req: CreateVideoPullTaskRequest):
    """创建定时拉取任务。"""
    try:
        return material_service.create_pull_task(
            req.task_name, req.conditions.model_dump(), req.account_id,
            req.category_id, req.interval.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/pull-tasks", summary="视频拉取任务列表")
def list_pull_tasks(page: int = 1, page_size: int = 20):
    """任务列表。"""
    return material_service.list_pull_tasks(page, page_size)


@router.put("/pull-tasks/{task_id}", summary="编辑视频拉取任务")
def update_pull_task(task_id: str, req: UpdateVideoPullTaskRequest):
    """编辑（下一轮生效）。"""
    try:
        return material_service.update_pull_task(
            task_id, req.task_name,
            req.conditions.model_dump() if req.conditions else None,
            req.account_id, req.category_id,
            req.interval.model_dump() if req.interval else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/pull-tasks/{task_id}/toggle", summary="任务启停")
def toggle_pull_task(task_id: str, req: ToggleRequest):
    """启用/停用。"""
    try:
        return material_service.toggle_pull_task(task_id, req.enabled)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/pull-tasks/{task_id}/restart", summary="v22 阶梯重置并重新启用")
def restart_pull_task(task_id: str):
    """v22：清零 pull_round + total_pulled，重新启用任务。

    适用场景：任务已达 max_count 自动停用，前端操作列显示"重新启用"按钮。
    重置后下次调度按第 1 轮 100 页跑起（阶梯公式：BASE-STEP*round，MIN 兜底）。
    """
    try:
        return material_service.restart_pull_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/pull-tasks/{task_id}/run", summary="手动触发一轮")
def run_pull_task_now(task_id: str):
    """立即投递执行。"""
    try:
        return {"ok": True, "bg_task_id": material_service.run_pull_task_now(task_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/pull-tasks/{task_id}/logs", summary="视频拉取执行记录")
def list_pull_task_logs(task_id: str, page: int = 1, page_size: int = 20):
    """查询任务每轮执行日志。"""
    return material_service.list_pull_logs(task_id, page, page_size)


@router.get("/pull-logs", summary="拉取执行记录聚合列表")
def list_all_pull_logs(page: int = 1, page_size: int = 20):
    """聚合所有拉取任务的执行记录（按时间倒序，含任务名）。"""
    return material_service.list_pull_logs(None, page, page_size)


@router.delete("/pull-logs/{log_id}", summary="删除单条拉取执行日志")
def delete_pull_log(log_id: str):
    """#351：聚合列表批量删除单条日志（前端并发调用）。"""
    try:
        material_service.delete_pull_log(log_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/pull-tasks/{task_id}", summary="删除视频拉取任务")
def delete_pull_task(task_id: str):
    """删除任务（已入库素材保留）。"""
    try:
        material_service.delete_pull_task(task_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------- 分享链接导入 / 上传 ----------

@router.post("/import-share", summary="创建分享链接导入任务（异步）")
def import_share(req: ImportShareRequest):
    """创建异步导入任务，立即返回任务 ID。"""
    try:
        task_id = create_share_import_task(req.category_id, req.share_texts, req.type)
        return {"task_id": task_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/import-tasks", summary="分享导入任务列表")
def list_import_tasks(page: int = 1, page_size: int = 20):
    """分页查询历史分享导入任务。"""
    return list_share_import_tasks(page, page_size)


@router.get("/import-tasks/{task_id}", summary="分享导入任务进度")
def get_import_task(task_id: str):
    """查询任务进度与明细。"""
    task = get_share_import_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task, "items": list_share_import_items(task_id)}


@router.post("/import-tasks/{task_id}/retry", summary="重试失败项")
def retry_import_task(task_id: str, req: RetryShareRequest):
    """对任务中失败的分享文本重新执行导入。

    #fix-type-param：retry 也需 type 入参（原任务 type 由前端页签决定）。
    """
    try:
        count = retry_share_import_task(task_id, type=req.type)
        return {"ok": True, "retry_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/import-tasks/{task_id}/items/{item_id}/retry", summary="重试单条失败项")
def retry_import_item(task_id: str, item_id: str, req: RetryShareRequest):
    """对单条失败的分享导入项重新执行导入。

    #fix-type-param：retry 也需 type 入参。
    """
    try:
        retry_share_import_item(task_id, item_id, type=req.type)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/import-tasks/{task_id}", summary="删除分享导入任务")
def delete_import_task(task_id: str):
    """删除任务及其明细（执行中不可删）。"""
    try:
        delete_share_import_task(task_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/upload-tasks", summary="上传任务列表")
def list_upload_tasks_route(page: int = 1, page_size: int = 20):
    """分页查询历史本地上传任务。"""
    return list_upload_tasks(page, page_size)


@router.post("/upload", summary="创建本地文件/文件夹上传任务（异步）")
def upload_files(req: UploadFilesRequest):
    """创建异步上传任务，递归处理文件夹，立即返回任务 ID。

    #123：返回 task_id + bg_task_id——
    task_id 是 upload_task 主键（业务查询用）；
    bg_task_id 是 task_service 队列 UUID（前端取消按钮 /tasks/{bg_id}/cancel 用）。
    """
    try:
        task_id, bg_task_id = create_upload_task(req.category_id, req.file_paths)
        return {"task_id": task_id, "bg_task_id": bg_task_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/upload-tasks/{task_id}", summary="上传任务进度")
def get_upload_task(task_id: str):
    """查询上传任务进度与明细。"""
    task = _get_upload_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task, "items": _list_upload_items(task_id)}


@router.post("/upload-tasks/{task_id}/retry", summary="重试上传失败项")
def retry_upload_task_route(task_id: str):
    """对任务中失败的文件重新上传。"""
    try:
        count = _retry_upload_task(task_id)
        return {"ok": True, "retry_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/upload-tasks/{task_id}/items/{item_id}/retry", summary="重试单条上传失败项")
def retry_upload_item(task_id: str, item_id: str):
    """对单条失败的上传项重新上传。"""
    try:
        _retry_upload_item(task_id, item_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/upload-tasks/{task_id}", summary="删除上传任务")
def delete_upload_task(task_id: str):
    """删除上传任务及其明细（执行中不可删）。"""
    try:
        _delete_upload_task(task_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- 素材列表与维护 ----------

@router.get("", summary="素材分页列表")
def list_materials(type: str = "", category_id: str = "", source_type: str = "",
                   file_status: str = "", keyword: str = "", orientation: str = "",
                   page: int = 1, page_size: int = 20):
    """多条件筛选列表。"""
    filters = {k: v for k, v in {
        "type": type, "category_id": category_id, "source_type": source_type,
        "file_status": file_status, "keyword": keyword, "orientation": orientation,
    }.items() if v}
    return material_service.list_materials(filters, page, page_size)


@router.put("/{material_id}", summary="素材编辑（标题/分类）")
def update_material(material_id: str, req: UpdateMaterialRequest):
    """重命名/移动分类。"""
    material_service.update_material(material_id, req.title, req.category_id)
    return {"ok": True}


@router.delete("/{material_id}", summary="删除素材")
def delete_material(material_id: str, keep_file: bool = False):
    """删除（可选保留文件）。"""
    try:
        material_service.delete_material(material_id, keep_file)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{material_id}/relocate", summary="重新定位文件")
def relocate_material(material_id: str, req: RelocateMaterialRequest):
    """缺失素材重新定位（F-03-R8）。"""
    try:
        material_service.relocate_material(material_id, req.new_abs_path)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{material_id}/abs-path", summary="获取素材绝对路径")
def get_abs_path(material_id: str):
    """素材文件绝对路径（前端调 shell 打开所在目录）。"""
    from app.services.material_service import full_path_of, get_material
    row = get_material(material_id)
    if not row:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"abs_path": str(full_path_of(row)), "exists": full_path_of(row).exists()}
