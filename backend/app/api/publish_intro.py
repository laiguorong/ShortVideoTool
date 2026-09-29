# -*- coding: utf-8 -*-
"""视频简介 + 项目-门店 绑定路由（#413 发布管理重设计）。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import publish_intro_service

router = APIRouter(prefix="/publish/intro", tags=["发布-简介/项目门店"])


# ---------- 视频简介 ----------

class IntroUpsertRequest(BaseModel):
    project_id: str
    content: str = Field(min_length=1)
    topics: list[str] = []


class IntroUpdateRequest(BaseModel):
    content: str | None = None
    topics: list[str] | None = None


class IntroSetRequest(BaseModel):
    """#415：富文本编辑器一次性提交所有标题/话题（全量覆盖）。
    每条就是一行字符串（content 已含 #话题，由前端富文本按行解析）。"""
    items: list[str] = Field(default_factory=list)


@router.get("/projects/{project_id}/intros", summary="项目简介列表")
def list_intros(project_id: str):
    return publish_intro_service.list_intros(project_id)


@router.post("/intros", summary="新增简介")
def create_intro(req: IntroUpsertRequest):
    try:
        return publish_intro_service.create_intro(req.project_id, req.content, req.topics)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/intros/{intro_id}", summary="更新简介")
def update_intro(intro_id: str, req: IntroUpdateRequest):
    try:
        n = publish_intro_service.update_intro(intro_id, req.content, req.topics)
        return {"ok": True, "updated": n}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/intros/{intro_id}", summary="删除简介")
def delete_intro(intro_id: str):
    n = publish_intro_service.delete_intro(intro_id)
    return {"ok": True, "deleted": n}


@router.put("/projects/{project_id}/intros", summary="#415 富文本编辑全量覆盖")
def set_project_intros(project_id: str, req: IntroSetRequest):
    """事务内清旧 + 重建，传什么就保存什么；空 items = 全删。"""
    try:
        n = publish_intro_service.set_project_intros(project_id, req.items)
        return {"ok": True, "saved": n}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- 项目-门店 绑定 ----------

class ProjectShopSetRequest(BaseModel):
    # 空数组语义 = 全删（#414 全删全加覆盖语义）
    shop_ids: list[str] = Field(default_factory=list)


@router.get("/projects/{project_id}/shops", summary="项目已绑门店列表（按 sort_order）")
def list_project_shops(project_id: str):
    return publish_intro_service.list_project_shops(project_id)


@router.put("/projects/{project_id}/shops", summary="全量覆盖项目门店绑定")
def set_project_shops(project_id: str, req: ProjectShopSetRequest):
    try:
        n = publish_intro_service.set_project_shops(project_id, req.shop_ids)
        return {"ok": True, "bound": n}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/projects-with-shops", summary="项目列表 + 已绑门店数（向导用）")
def list_projects_with_shops():
    return publish_intro_service.list_projects_with_shops()


# ---------- #416：项目停用/启用 ----------


class ProjectStatusRequest(BaseModel):
    """#416：项目停用/启用。status='disabled' 停用、='normal' 启用。
    停用后新建发布任务（向导）不能再选该项目；停用不影响已有任务/成品/分镜。"""
    status: str = Field(pattern="^(normal|disabled)$")


@router.put("/projects/{project_id}/status", summary="#416 停用/启用项目")
def set_project_status(project_id: str, req: ProjectStatusRequest):
    try:
        n = publish_intro_service.set_project_status(project_id, req.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if n == 0:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"ok": True, "status": req.status}


@router.get("/projects-publishable", summary="#416 可发布项目列表（status=normal）")
def list_publishable_projects():
    """新建发布任务时调：仅返回 status='normal' 的项目（停用的不会出现在向导中）。"""
    return publish_intro_service.list_publishable_projects()