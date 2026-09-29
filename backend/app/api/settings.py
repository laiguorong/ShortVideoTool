# -*- coding: utf-8 -*-
"""系统设置路由（F-08）：配置、健康检查、清理、备份恢复、风险告知、通知中心。"""

from fastapi import APIRouter, HTTPException

from app.core import notifier
from app.models.setting import (BackupRequest, DataDirMigrateRequest, RestoreRequest,
                                RiskConfirmRequest, SettingsUpdateRequest)
from app.services import setting_service

router = APIRouter(prefix="/settings", tags=["系统设置"])


@router.get("", summary="读取全局配置")
def get_settings():
    """读取全局配置。"""
    return setting_service.load_settings()


@router.put("", summary="修改全局配置（热加载）")
def update_settings(req: SettingsUpdateRequest):
    """合并保存配置，即时生效。"""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    saved = setting_service.save_settings(updates)
    if "account_check_hours" in updates:
        from app.services.account_service import register_account_check_job
        register_account_check_job()
    return saved


@router.get("/health-check", summary="目录健康检查")
def health_check():
    """子目录齐全性/可写性/磁盘剩余。"""
    return setting_service.health_check()


@router.post("/cleanup-temp-cache", summary="清理 temp 与 cache")
def cleanup():
    """清理缓存与临时文件。"""
    return setting_service.cleanup_temp_cache()


@router.post("/backup", summary="备份 db+config")
def backup(req: BackupRequest):
    """备份（可选含素材/成品）。"""
    try:
        return setting_service.backup(req.target_path, req.include_material, req.include_finished)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"备份失败：{e}") from e


@router.post("/restore", summary="从备份恢复")
def restore(req: RestoreRequest):
    """恢复（恢复后需重启应用）。"""
    try:
        return setting_service.restore(req.zip_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复失败：{e}") from e


@router.get("/data-dir", summary="获取数据目录路径")
def get_data_dir():
    """当前 data 目录与子目录清单。"""
    from app.services.setting_service import DATA_SUB_DIRS, get_data_dir as _dir
    return {"data_dir": str(_dir()), "sub_dirs": DATA_SUB_DIRS}


@router.get("/face-evidence-dir", summary="人脸证据目录路径")
def face_evidence_dir():
    """人脸检测命中帧证据目录：data/cache/face_evidence/。

    人工复检人脸可疑视频用。命中证据由 media_inspect._save_face_evidence 写入
    （任务 #129 复盘修复）；目录在 DATA_TEMP_SUBDIRS（cache）下，由
    cleanup_temp_cache / cleanup_startup_temp 清理。
    """
    from app.services.setting_service import get_data_dir as _dir
    return {"face_evidence_dir": str(_dir() / "cache" / "face_evidence")}


# ---------- 首次启动风险告知 ----------

@router.get("/risk-confirmed", summary="查询风险告知是否已确认")
def risk_confirmed():
    """首次启动风险告知确认状态。"""
    value = setting_service.get_app_state("risk_confirmed")
    return {"confirmed": value == "1"}


@router.post("/risk-confirm", summary="确认风险告知")
def risk_confirm(req: RiskConfirmRequest):
    """确认（拒绝由前端直接退出应用）。"""
    setting_service.set_app_state("risk_confirmed", "1" if req.confirmed else "0")
    return {"ok": True}


# ---------- 通知中心（3.3-6） ----------

@router.get("/notifications", summary="通知列表")
def list_notifications(only_unread: bool = False, page: int = 1, page_size: int = 50):
    """通知分页列表（时间倒序）。"""
    return notifier.list_notifications(only_unread, page, page_size)


@router.post("/notifications/read", summary="标记已读")
def mark_read(notification_id: str = "", all_read: bool = False):
    """单条或全部标记已读。"""
    notifier.mark_read(notification_id, all_read)
    return {"ok": True}


@router.post("/notifications/{notification_id}/handled", summary="标记已处理")
def mark_handled(notification_id: str):
    """执行动作后标记已处理。"""
    notifier.mark_handled(notification_id)
    return {"ok": True}
