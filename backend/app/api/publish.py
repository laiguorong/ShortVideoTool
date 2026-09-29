# -*- coding: utf-8 -*-
"""发布中心与发布记录路由（F-05 / F-06）。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.publish import (CreatePublishTaskRequest, DraftRequest,
                                ExportRecordsRequest, ToggleTaskRequest)
from app.services import publish_service

router = APIRouter(prefix="/publish", tags=["发布中心"])


# ---------- #418 成品视频目录扫描 ----------

class ScanVideoDirRequest(BaseModel):
    abs_path: str


@router.post("/video-dirs/scan", summary="#418 扫描成品视频目录（校验合规视频数）")
def scan_video_dir(req: ScanVideoDirRequest):
    """扫描目录返回 {abs_path, dir_name, video_count, video_files[]}。
    失败抛 ValueError → 前端 toast 错误（HTTP 400）。
    成功后自动 remember 上次路径。
    """
    from app.services.video_dir_service import scan_video_dir as _scan, remember_last_dir
    try:
        info = _scan(req.abs_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    remember_last_dir(req.abs_path)
    return info


@router.get("/video-dirs/last", summary="#418 获取上次选择的目录路径")
def get_last_video_dir():
    from app.services.video_dir_service import get_last_dir
    return {"abs_path": get_last_dir()}


# ---------- #413 向导三段：草稿 → 预览 → 确认 ----------

@router.post("/tasks/draft", summary="保存草稿（向导任意一步可调）")
def save_draft(req: DraftRequest):
    """保存草稿到 publish_task(status=draft)；R12：传 task_id 则更新已有 draft。"""
    try:
        # #新形态归一：account_snapshots → account_ids（preview-direct 走 _preview_video_dir/_normalize_payload 也有，save_draft 补齐）
        payload = req.model_dump()
        publish_service._normalize_payload(payload)
        account_ids = payload.get("account_ids") or []
        return publish_service.save_draft(
            task_name=req.task_name,
            projects_payload=[p.model_dump() for p in req.projects],
            account_ids=account_ids,
            daily_limit_mode=req.daily_limit_mode,
            daily_limit_global=req.daily_limit_global,
            daily_limit_per_account=[(e.account_id, e.limit) for e in req.daily_limit_per_account],
            start_time=req.start_time,
            end_time=req.end_time,
            same_project_interval_min=req.same_project_interval_min,
            diff_project_interval_min=req.diff_project_interval_min,
            declaration=req.declaration,
            allow_download=req.allow_download,
            task_id=req.task_id,
            project_source=req.project_source or "project",
            video_dirs=[d.model_dump() for d in (req.video_dirs or [])],
            manual_shops={k: [s.model_dump() for s in v] for k, v in (req.manual_shops or {}).items()},
            # 视频简介-按项目一一对应：{dir_id: [str, ...]}
            manual_intros={k: list(v) for k, v in (req.manual_intros or {}).items()},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/tasks/{task_id}/preview", summary="计算排期预览（不入库）")
def preview_task(task_id: str):
    """调 build_schedule，返回 items 列表 + 汇总；不入库不占成品。"""
    try:
        return publish_service.preview_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/confirm", summary="确认草稿 → 落库明细 + 占成品 + running")
def confirm_task(task_id: str):
    """向导第 8 步：用户确认后调用。"""
    try:
        return publish_service.confirm_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- #441 直传 payload 版预览/确认（wizard 临时化） ----------

@router.post("/tasks/preview-direct", summary="#441 直传 payload 预览（不入库）")
def preview_direct(req: DraftRequest):
    """wizard 临时化版预览：不依赖 task_id，不写 DB。
    包含：硬校验（账号/项目/门店/简介/排期有效性）→ 构造 ScheduleConfig → build_schedule。
    响应结构兼容旧 preview_task（items/stats/validation/overflow）。
    旧 /publish/tasks/draft + /preview 流程保留向后兼容（已落库的草稿任务可继续编辑）。"""
    try:
        return publish_service.preview_task_direct(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/confirm-direct", summary="#441 直传 payload 确认（一次性入库）")
def confirm_direct(req: DraftRequest):
    """wizard 临时化版确认：直接接 payload，事务内全流程：
    硬校验 → INSERT publish_task → build_schedule → 水量校验 → 占成品 → 落明细 → status='running'。
    不需要先 save_draft 落草稿。旧 /publish/tasks/{id}/confirm 保留向后兼容。"""
    try:
        return publish_service.confirm_task_direct(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/duplicate", summary="复制任务为新草稿")
def duplicate_task(task_id: str):
    """复制源任务所有配置为新草稿（status=draft），不展开明细。"""
    try:
        return publish_service.duplicate_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/tasks/{task_id}/detail", summary="任务只读详情（#413）")
def get_task_detail_ro(task_id: str):
    """只读视图，语义上不再支持编辑。"""
    try:
        return publish_service.get_task_detail_ro(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------- 旧接口保留（向后兼容） ----------

@router.post("/tasks", summary="[旧] 创建发布任务（向导请走 draft/preview/confirm）")
def create_task(req: CreatePublishTaskRequest):
    """预校验→生成排期→取用成品。"""
    try:
        return publish_service.create_task(
            req.task_name, req.project_ids, req.account_ids, req.shop_ids,
            req.per_account_count, req.start_time, req.interval_minutes, req.shop_assign_mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/tasks", summary="任务列表")
def list_tasks(page: int = 1, page_size: int = 20,
               status: str | None = None):
    """任务列表（进度汇总）。
    C3：按状态过滤。"""
    return publish_service.list_tasks(page, page_size, status=status)


@router.get("/tasks/{task_id}", summary="任务详情（含明细）")
def get_task(task_id: str):
    """任务详情与全部发布明细。"""
    try:
        return publish_service.get_task_detail(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/tasks/{task_id}/toggle", summary="任务暂停/恢复")
def toggle_task(task_id: str, req: ToggleTaskRequest):
    """暂停期间到期明细顺延。"""
    try:
        return publish_service.toggle_task(task_id, req.paused)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
def cancel_task(task_id: str):
    """未执行明细取消并释放成品。"""
    try:
        n = publish_service.cancel_task(task_id)
        return {"ok": True, "cancelled_items": n}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/tasks/{task_id}", summary="删除已终态任务")
def delete_task(task_id: str):
    """仅终态可删；发布记录永久保留。"""
    try:
        publish_service.delete_task(task_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/retry", summary="重试任务下所有失败明细")
def retry_task(task_id: str):
    """任务级重试：按 plan_time 阈值分流（≥2h 复活 waiting，<2h 永久关闭）。"""
    try:
        result = publish_service.retry_task(task_id)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/restart", summary="重启已取消任务")
def restart_task(task_id: str):
    """#优化：cancelled 任务回 running，明细重置回 waiting 重新派发。"""
    try:
        result = publish_service.restart_task(task_id)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- 发布记录（F-06） ----------

@router.get("/records", summary="发布记录列表")
def list_records(account_id: str = "", shop_id: str = "", project_id: str = "",
                 start_time: str = "", end_time: str = "",
                 page: int = 1, page_size: int = 20,
                 keyword: str = ""):
    """发布记录分页（#456：keyword 入参触发实时查询）。"""
    return publish_service.list_records(account_id, shop_id, project_id,
                                        start_time, end_time, page, page_size,
                                        keyword=keyword)


@router.post("/records/export", summary="导出 Excel")
def export_records(req: ExportRecordsRequest):
    """按筛选导出 xlsx。"""
    try:
        path = publish_service.export_records(
            {"account_id": req.account_id, "shop_id": req.shop_id,
             "project_id": req.project_id, "start_time": req.start_time,
             "end_time": req.end_time},
            req.out_path)
        return {"ok": True, "file": path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败：{e}") from e


@router.get("/records/{record_id}/abs-path", summary="发布记录视频绝对路径（兼容两种模式）")
def get_record_abs_path(record_id: str):
    """#bugfix：成品视频目录模式 video_id='' → /creation/videos/{id}/abs-path 永远 404。

    按 record_id 直接读 publish_record.video_local_path（项目模式为相对 data_dir，
    成品目录模式为绝对路径），统一返回 abs_path + exists。

    路径解析：相对路径（不以盘符/UNC 开头）→ get_data_dir()/rel；绝对路径→原样。

    #bugfix2：历史记录 video_local_path 是 move_to_published 之前的旧路径（文件已搬走），
    自动 fallback 到 `<parent>/_published/<name>`（视频目录模式发布后归档子目录）。
    """
    from pathlib import Path
    from app.services.setting_service import get_data_dir
    # 直接拿 db 单例（publish_service 用 from app.db import get_db）
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
    # 相对路径（项目模式：create/<pid>/xxx.mp4）→ 拼 data_dir
    if not p.is_absolute():
        p = get_data_dir() / raw
    if p.exists():
        return {"abs_path": str(p), "exists": True}
    # 历史数据 fallback：视频目录模式发布后文件被移到 <parent>/_published/<name>
    parent = p.parent
    name = p.name
    if parent.name != "_published":
        candidate = parent / "_published" / name
        if candidate.exists():
            return {"abs_path": str(candidate), "exists": True}
    return {"abs_path": str(p), "exists": False}
