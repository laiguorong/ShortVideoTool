# -*- coding: utf-8 -*-
"""创作中心路由（F-04）。"""

from fastapi import APIRouter, HTTPException

from app.models.creation import (AddBgmsRequest, AddClipsRequest, AddShotRequest,
                                 BatchClipsRequest,
                                 AddTextPoolRequest, CopyProjectRequest,
                                 CreateGenerateTaskRequest, CreateProjectRequest,
                                 DeleteByMaterialsRequest,
                                 MoveClipRequest, MoveShotRequest, ReorderClipsRequest,
                                 RenameShotRequest, UpdateProjectRequest)
from app.db import get_db
from app.services import creation_service

router = APIRouter(prefix="/creation", tags=["创作中心"])


# ---------- 项目 ----------

@router.post("/projects", summary="创建项目")
def create_project(req: CreateProjectRequest):
    """自定义标题新建项目（#427：可指定 width/height 画幅 + fit_mode）。"""
    return creation_service.create_project(
        req.title, req.width or 1080, req.height or 1920,
        req.fit_mode or "cover")


@router.get("/resolution-presets", summary="画幅预设列表（#426）")
def resolution_presets():
    """横竖屏/分辨率预设（前端 UI 选择用）。"""
    return creation_service.RESOLUTION_PRESETS


@router.get("/projects", summary="项目列表")
def list_projects(page: int = 1, page_size: int = 20):
    """项目列表（含组合总数/已生成数）。"""
    return creation_service.list_projects(page, page_size)


@router.get("/projects/{project_id}", summary="项目详情")
def get_project(project_id: str):
    """详情（含分镜/片段/BGM）。"""
    try:
        return creation_service.get_project(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/projects/{project_id}/clips-status", summary="片段状态差异轮询")
def clips_status(project_id: str, since_rev: int = 0):
    """返回 rev > since_rev 的片段（差异轮询，#轮询优化）。

    参数:
        since_rev: 上次响应的 max_rev，0 = 全量（首次调用）。
    返回:
        {"clips": [...], "max_rev": int, "pending": int, "failed": int}
        clips 字段只含渲染状态相关列,不带 material JOIN。
    """
    return creation_service.clips_status(project_id, since_rev)


@router.put("/projects/{project_id}", summary="更新项目配置")
def update_project(project_id: str, req: UpdateProjectRequest):
    """标题/BGM 策略/去重规则/输出分辨率/画面适配。"""
    try:
        return creation_service.update_project(
            project_id, req.title, req.bgm_strategy, req.dedup_rules, req.output_resolution,
            req.keep_original_audio, req.bgm_volume, req.fit_mode,
            req.width, req.height)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/projects/{project_id}/copy", summary="复制项目")
def copy_project(project_id: str, req: CopyProjectRequest):
    """分镜/片段/BGM 全拷贝。"""
    try:
        return creation_service.copy_project(project_id, req.new_title)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/projects/{project_id}", summary="删除项目")
def delete_project(project_id: str, delete_files: bool = False):
    """删除项目（成品文件可选一并删除）。"""
    try:
        creation_service.delete_project(project_id, delete_files)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------- 分镜 ----------

@router.post("/projects/{project_id}/shots", summary="添加/插入分镜")
def add_shot(project_id: str, req: AddShotRequest):
    """上限 50；before/after_shot_id 指定插入位置（#62/#67）。"""
    try:
        return creation_service.add_shot(project_id, req.name,
                                         req.after_shot_id, req.before_shot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/shots/{shot_id}/rename", summary="分镜重命名")
def rename_shot(shot_id: str, req: RenameShotRequest):
    """重命名。"""
    creation_service.rename_shot(shot_id, req.name)
    return {"ok": True}


@router.post("/shots/{shot_id}/move", summary="分镜上移/下移")
def move_shot(shot_id: str, req: MoveShotRequest):
    """相邻交换。"""
    try:
        creation_service.move_shot(shot_id, req.direction)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/shots/{shot_id}", summary="删除分镜")
def delete_shot(shot_id: str):
    """删除分镜及其片段。"""
    try:
        creation_service.delete_shot(shot_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------- 片段 ----------

@router.post("/shots/add-clips", summary="批量添加片段（含切割）")
def add_clips(req: AddClipsRequest):
    """素材多选 + 切割方式 + 自动按分镜序分配（F-04.5 / #56）。"""
    try:
        return creation_service.add_clips_from_materials(
            req.project_id, req.material_ids, req.cut_mode, req.fixed_seconds,
            req.trim_head_s, req.trim_tail_s, req.min_clip_seconds, req.target_shot_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/clips/{clip_id}/move", summary="片段移动到其他分镜")
def move_clip(clip_id: str, req: MoveClipRequest):
    """移动。"""
    try:
        creation_service.move_clip(clip_id, req.target_shot_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/clips/{clip_id}", summary="删除片段")
def delete_clip(clip_id: str):
    """删除。"""
    creation_service.delete_clip(clip_id)
    return {"ok": True}


@router.post("/clips/{clip_id}/mirror", summary="镜像片段")
def mirror_clip(clip_id: str):
    """生成镜像新片段（原片段保留）。"""
    try:
        return creation_service.mirror_clip(clip_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/shots/{shot_id}/reorder-clips", summary="分镜内片段重排")
def reorder_clips(shot_id: str, req: ReorderClipsRequest):
    """拖拽换位重排（#56）。"""
    try:
        creation_service.reorder_clips(shot_id, req.clip_ids)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/clips/batch", summary="片段批量操作")
def batch_clips(req: BatchClipsRequest):
    """批量删除/镜像/移动到其他分镜/重渲染（#57 分镜管理模式）。"""
    try:
        return creation_service.batch_clips(req.clip_ids, req.action, req.target_shot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/clips/{clip_id}/render", summary="重新渲染片段文件")
def render_clip(clip_id: str):
    """失败重试 / 缩略图恢复（#57）。"""
    try:
        return creation_service.render_clip(clip_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/projects/{project_id}/delete-by-materials", summary="按素材批量删除项目内片段")
def delete_by_materials(project_id: str, req: DeleteByMaterialsRequest):
    """#399：项目内按 material_ids 删全部对应片段（含文件）。

    不动素材库本身（其他项目可能仍引用）；仅清 project_shot_clip 行与落盘片段/缩略图文件。
    """
    deleted = creation_service.delete_clips_by_material(project_id, req.material_ids)
    return {"ok": True, "deleted": deleted}


# ---------- BGM ----------

@router.post("/projects/{project_id}/bgms", summary="批量添加 BGM")
def add_bgms(project_id: str, req: AddBgmsRequest):
    """音乐素材批量加入 BGM 池（音量走项目级）。"""
    added = creation_service.add_bgms(project_id, req.material_ids)
    return {"ok": True, "added": added}


@router.delete("/projects/{project_id}/bgms", summary="移除 BGM")
def remove_bgm(project_id: str, material_id: str):
    """移除。"""
    creation_service.remove_bgm(project_id, material_id)
    return {"ok": True}


@router.get("/projects/{project_id}/bgms", summary="BGM 池列表")
def list_bgms(project_id: str):
    """BGM 列表。"""
    return creation_service.list_bgms(project_id)


# ---------- 文案池 ----------

@router.get("/text-pool", summary="文案池列表")
def list_text_pool(pool_type: str, scope: str = "global", project_id: str = ""):
    """标题池/话题池（全局/项目两层）。"""
    return creation_service.list_text_pool(pool_type, scope, project_id or None)


@router.post("/text-pool", summary="批量添加文案")
def add_text_pool(pool_type: str, req: AddTextPoolRequest):
    """每行一条批量添加（元素可含换行，此处展开）。"""
    lines = [line for block in req.contents for line in block.split("\n")]
    added = creation_service.add_text_pool(pool_type, lines, req.scope, req.project_id)
    return {"ok": True, "added": added}


@router.delete("/text-pool/{entry_id}", summary="删除文案条目")
def delete_text_pool(entry_id: str):
    """删除。"""
    creation_service.delete_text_pool(entry_id)
    return {"ok": True}


# ---------- 生成与成品 ----------

@router.post("/projects/{project_id}/generate", summary="创建生成任务")
def create_generate_task(project_id: str, req: CreateGenerateTaskRequest):
    """组合生成（组合键唯一防重复）。"""
    try:
        return creation_service.create_generate_task(project_id, req.count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/videos", summary="成品视频列表")
def list_videos(project_id: str = "", status: str = "", page: int = 1, page_size: int = 20):
    """成品列表（F-04.10）。"""
    return creation_service.list_generated_videos(project_id, status, page, page_size)


@router.delete("/videos/{video_id}", summary="删除成品")
def delete_video(video_id: str, keep_file: bool = False):
    """仅未占用可删（组合键释放）。"""
    try:
        creation_service.delete_generated_video(video_id, keep_file)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/videos/{video_id}/abs-path", summary="获取成品绝对路径")
def get_video_abs_path(video_id: str):
    """成品文件绝对路径（前端 shell 定位）。"""
    from app.services.setting_service import get_data_dir
    d = get_db()
    row = d.query_one("SELECT file_path FROM generated_video WHERE id=?", (video_id,))
    if not row:
        raise HTTPException(status_code=404, detail="成品不存在")
    p = get_data_dir() / row["file_path"]
    return {"abs_path": str(p), "exists": p.exists()}
