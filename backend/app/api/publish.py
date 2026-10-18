# -*- coding: utf-8 -*-
"""发布中心与发布记录路由（#publish-contract 全面标准化）。

统一约定：
- 成功响应全部包 `ApiResp[data=...]` 信封（code=0）。
- 业务/校验错误仍抛 HTTPException(4xx)，由前端 client 按 status 分类。
- 向导请求仅用对象数组形态（project_snapshots / account_snapshots），不再有旧形态。
- 已删除未使用旧接口：POST /tasks(create_task)、GET /tasks/{id}(get_task)、
  GET /tasks/{id}/preview(preview_task)。
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.models.common import ok, PageQuery
from app.models.publish import (
    ConfirmRequest,
    ExportRecordsRequest,
    PreviewRequest,
    SaveDraftRequest,
    ScanVideoDirRequest,
    ToggleTaskRequest,
)
from app.services import publish_service, publish_intro_service

router = APIRouter(prefix="/publish", tags=["发布中心"])


# ---------- #418 成品视频目录扫描 ----------

@router.post("/video-dirs/scan", summary="#418 扫描成品视频目录（校验合规视频数）")
def scan_video_dir(req: ScanVideoDirRequest):
    """扫描目录返回 {abs_path, dir_name, video_count, video_files[]}。失败抛 ValueError → 400。"""
    from app.services.video_dir_service import scan_video_dir as _scan
    info = _scan(req.abs_path)
    return ok(info)


# ---------- #413 向导三段：草稿 → 预览 → 确认 ----------

@router.post("/tasks/draft", summary="保存草稿（向导任意一步可调）")
def save_draft(req: SaveDraftRequest):
    """保存草稿到 publish_task(status=draft)；传 task_id 则更新已有 draft。"""
    # 归一化：对象数组快照 → service 内部统一 projects/account_ids
    payload = req.model_dump()
    publish_service._normalize_payload(payload)
    result = publish_service.save_draft(
    task_name=req.task_name,
    projects_payload=payload.get("projects") or [],
    account_ids=payload.get("account_ids") or [],
    daily_limit_mode=req.daily_limit_mode,
    daily_limit_global=req.daily_limit_global,
    daily_limit_per_account=[e.model_dump() for e in req.daily_limit_per_account],
    start_time=req.start_time,
    end_time=req.end_time,
    declaration=req.declaration,
    allow_download=req.allow_download,
    task_id=req.task_id,
    project_source=req.project_source,
    video_dirs=[d.model_dump() for d in (req.video_dirs or [])],
    manual_shops={k: [s.model_dump() for s in v] for k, v in (req.manual_shops or {}).items()},
    manual_intros={k: list(v) for k, v in (req.manual_intros or {}).items()},
    )
    return ok(result)


@router.get("/tasks/{task_id}/preview", summary="计算排期预览（不入库）")
def preview_task(task_id: str):
    """调 build_schedule，返回 items 列表 + 汇总；不入库不占成品。"""
    return ok(publish_service.preview_task(task_id))


@router.post("/tasks/{task_id}/confirm", summary="确认草稿 → 落库明细 + 占成品 + running")
def confirm_task(task_id: str):
    """向导第 8 步：用户确认后调用。"""
    return ok(publish_service.confirm_task(task_id))


# ---------- #441 直传 payload 版预览/确认（wizard 临时化） ----------

@router.post("/tasks/preview-direct", summary="#441 直传 payload 预览（不入库）")
def preview_direct(req: PreviewRequest):
    """wizard 临时化版预览：不依赖 task_id，不写 DB。响应兼容 PreviewResult（task_id=None）。"""
    return ok(publish_service.preview_task_direct(req.model_dump()))


@router.post("/tasks/confirm-direct", summary="#441 直传 payload 确认（一次性入库）")
def confirm_direct(req: ConfirmRequest):
    """wizard 临时化版确认：直接接 payload，事务内全流程落库 → running。不需先 save_draft。"""
    return ok(publish_service.confirm_task_direct(req.model_dump()))


@router.post("/tasks/{task_id}/duplicate", summary="复制任务为新草稿")
def duplicate_task(task_id: str):
    """复制源任务所有配置为新草稿（status=draft），不展开明细。"""
    return ok(publish_service.duplicate_task(task_id))


@router.get("/tasks/{task_id}/detail", summary="任务只读详情（#413）")
def get_task_detail_ro(task_id: str):
    """只读视图，语义上不再支持编辑。"""
    return ok(publish_service.get_task_detail_ro(task_id))


# ---------- 任务生命周期（操作类） ----------

class TaskListQuery(PageQuery):
    status: str | None = None
    account_id: str | None = None


@router.get("/tasks", summary="任务列表")
def list_tasks(
    query: Annotated[TaskListQuery, Query()],
):
    """任务列表（进度汇总）。按状态/账号过滤。

    #fix-pagequery-query-source：路由函数参数必须显式 Query() 标注，FastAPI 才从 query string 解析
    否则 Pydantic model 默认从 body 解析 → GET 无 body → 422。
    """
    return ok(publish_service.list_tasks(query.page, query.page_size, status=query.status, account_id=query.account_id))


@router.post("/tasks/{task_id}/toggle", summary="任务暂停/恢复")
def toggle_task(task_id: str, req: ToggleTaskRequest):
    """暂停期间到期明细顺延。"""
    return ok(publish_service.toggle_task(task_id, req.paused))


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
def cancel_task(task_id: str):
    """未执行明细取消并释放成品。"""
    n = publish_service.cancel_task(task_id)
    return ok({"cancelled_items": n})


@router.delete("/tasks/{task_id}", summary="删除已终态任务")
def delete_task(task_id: str):
    """仅终态可删；发布记录永久保留。"""
    publish_service.delete_task(task_id)
    return ok({})


@router.post("/tasks/{task_id}/retry", summary="重试任务下所有失败明细")
def retry_task(task_id: str):
    """任务级重试：按 plan_time 阈值分流（≥2h 复活 waiting，<2h 永久关闭）。"""
    result = publish_service.retry_task(task_id)
    return ok(result)


@router.post("/tasks/{task_id}/restart", summary="重启已取消任务")
def restart_task(task_id: str):
    """cancelled 任务回 running，明细重置回 waiting 重新派发。"""
    result = publish_service.restart_task(task_id)
    return ok(result)


# ---------- 发布记录（F-06） ----------

class RecordsPageQuery(PageQuery):
    account_id: str = ""
    shop_id: str = ""
    project_id: str = ""
    start_time: str = ""
    end_time: str = ""
    keyword: str = ""


@router.get("/records", summary="发布记录列表")
def list_records(
    query: Annotated[RecordsPageQuery, Query()],
):
    """发布记录分页（keyword 入参触发实时查询）。

    #fix-pagequery-query-source：见 list_tasks 注释
    """
    return ok(publish_service.list_records(query.account_id, query.shop_id, query.project_id,
                                            query.start_time, query.end_time, query.page, query.page_size,
                                            keyword=query.keyword))


@router.post("/records/export", summary="导出 Excel")
def export_records(req: ExportRecordsRequest):
    """按筛选导出 xlsx。"""
    try:
        path = publish_service.export_records(
            {"account_id": req.account_id, "shop_id": req.shop_id,
             "project_id": req.project_id, "start_time": req.start_time,
             "end_time": req.end_time},
            req.out_path)
        return ok({"file": path})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败：{e}") from e


@router.get("/records/{record_id}/abs-path", summary="发布记录视频绝对路径（兼容两种模式）")
def get_record_abs_path(record_id: str):
    """按 record_id 读 publish_record.video_local_path，统一返回 abs_path + exists。"""
    from pathlib import Path
    from app.services.setting_service import get_data_dir
    from app.db import get_db
    d = get_db()
    row = d.query_one(
        "SELECT video_local_path, video_id FROM publish_record WHERE id=?",
        (record_id,))
    if not row:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    raw = row["video_local_path"] or ""
    if not raw:
        raise HTTPException(status_code=404, detail="该记录无本地视频路径")
    p = Path(raw)
    if not p.is_absolute():
        p = get_data_dir() / raw
    if p.exists():
        return ok({"abs_path": str(p), "exists": True})
    parent = p.parent
    name = p.name
    if parent.name != "_published":
        candidate = parent / "_published" / name
        if candidate.exists():
            return ok({"abs_path": str(candidate), "exists": True})
    return ok({"abs_path": str(p), "exists": False})


# ---------- #413 视频简介 + 项目-门店绑定（#publish-contract：由 publish_intro 合并入发布域） ----------

class IntroUpsertRequest(BaseModel):
    project_id: str
    content: str = Field(min_length=1)
    topics: list[str] = []


class IntroUpdateRequest(BaseModel):
    content: str | None = None
    topics: list[str] | None = None


class IntroSetRequest(BaseModel):
    items: list[str] = Field(default_factory=list)


@router.get("/intro/projects/{project_id}/intros", summary="项目简介列表")
def list_intros(project_id: str):
    return ok(publish_intro_service.list_intros(project_id))


@router.post("/intro/intros", summary="新增简介")
def create_intro(req: IntroUpsertRequest):
    return ok(publish_intro_service.create_intro(req.project_id, req.content, req.topics))


@router.patch("/intro/intros/{intro_id}", summary="更新简介")
def update_intro(intro_id: str, req: IntroUpdateRequest):
    n = publish_intro_service.update_intro(intro_id, req.content, req.topics)
    return ok({"updated": n})


@router.delete("/intro/intros/{intro_id}", summary="删除简介")
def delete_intro(intro_id: str):
    n = publish_intro_service.delete_intro(intro_id)
    return ok({"deleted": n})


@router.put("/intro/projects/{project_id}/intros", summary="#415 富文本编辑全量覆盖")
def set_project_intros(project_id: str, req: IntroSetRequest):
    n = publish_intro_service.set_project_intros(project_id, req.items)
    return ok({"saved": n})


class ProjectShopSetRequest(BaseModel):
    shop_ids: list[str] = Field(default_factory=list)


@router.get("/intro/projects/{project_id}/shops", summary="项目已绑门店列表（按 sort_order）")
def list_project_shops(project_id: str):
    return ok(publish_intro_service.list_project_shops(project_id))


@router.put("/intro/projects/{project_id}/shops", summary="全量覆盖项目门店绑定")
def set_project_shops(project_id: str, req: ProjectShopSetRequest):
    n = publish_intro_service.set_project_shops(project_id, req.shop_ids)
    return ok({"bound": n})


@router.get("/intro/projects-with-shops", summary="项目列表 + 已绑门店数（向导用）")
def list_projects_with_shops():
    return ok(publish_intro_service.list_projects_with_shops())


class ProjectStatusRequest(BaseModel):
    status: str = Field(pattern="^(normal|disabled)$")


@router.put("/intro/projects/{project_id}/status", summary="#416 停用/启用项目")
def set_project_status(project_id: str, req: ProjectStatusRequest):
    n = publish_intro_service.set_project_status(project_id, req.status)
    if n == 0:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ok({"status": req.status})


@router.get("/intro/projects-publishable", summary="#416 可发布项目列表（status=normal）")
def list_publishable_projects():
    return ok(publish_intro_service.list_publishable_projects())
