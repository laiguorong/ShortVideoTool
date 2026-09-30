# -*- coding: utf-8 -*-
"""账号管理路由（F-01）。"""

import json

from fastapi import APIRouter, HTTPException

from app.models.account import (AddAccountRequest, ReloginRequest,
                                ToggleDisableRequest, UpdateRemarkRequest,
                                LoginWindowRequest)
from app.services import account_service

# 顶层 import：让 patch app.api.accounts.get_db / get_data_dir 可达（单测用）
from app.db import get_db
from app.services.setting_service import get_data_dir

router = APIRouter(prefix="/accounts", tags=["账号管理"])


@router.post("", summary="添加账号（登录窗抓取的 Cookie + 页面身份信息）")
def add_account(req: AddAccountRequest):
    """添加账号：cookie + 页面 evaluate 拿到的 profile 一起落库。

    #fix-profile-dir-move：登录窗临时 profile_dir 搬移到 accounts/{id}/profile/，
    确保后续 launch_persistent_context 能读到登录态（cookies + IndexedDB）。
    """
    profile = {
        "nickname": req.nickname,
        "douyin_id": req.douyin_id,
        "avatar": req.avatar,
    }
    try:
        return account_service.add_account(
            req.cookie, req.remark, profile=profile, profile_dir=req.profile_dir,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/login-window", summary="弹浏览器登录窗抓 Cookie（用于添加/重新登录）")
def open_login_window(req: LoginWindowRequest = LoginWindowRequest()):
    """用 Playwright headed Chromium 弹登录窗，等用户登录后返回 Cookie 串。

    #452：登录态持久化到 user_data_dir（profile_dir，chromium launch_persistent_context 用）；
    storage.json 同步备份（API 校验用）。

    参数（body 可选）:
        account_id: 已知账号 ID（重新登录场景）→ profile + storage.json 都写到账号目录
                    不传（添加账号场景）→ 用临时路径，cookie 串由前端拿走后自行管理

    返回:
        成功：{"cookie": "...", "nickname": "...", "douyin_id": "...", "avatar": "..."}
        失败：{"cookie": null, ...}
    """
    import os
    import tempfile
    from pathlib import Path

    from app.core.douyin.browser import browser_actor

    account_id = req.account_id if req else None

    if account_id:
        # 重新登录场景：profile_dir 持久化（#fix-unify-profile-storage 不再写 storage.json 备份）
        from app.services.douyin_account import get_profile_dir
        profile_dir = get_profile_dir(account_id)
        save_path = None  # 不写 storage.json（统一持久化路径后 storage.json 下线）
        tmp_path = None
    else:
        # 添加账号场景：临时路径（账号还没建，等拿到 cookie 后才决定落哪）
        profile_dir = Path(tempfile.mkdtemp(prefix="login_profile_"))
        save_path = None  # 不写 storage.json（统一持久化路径后 storage.json 下线）
        tmp_path = None

    try:
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",  # 入口用根域；用户扫码后跳到子路径也可（域判定宽松）
            user_data_dir=profile_dir,
            save_path=save_path,  # Path 对象（str 类型会触发 storage.json 备份 .parent 报错，参见 #bug-617）
            timeout_s=300,
            headless=False,
        )
        # result: dict | None（None = 超时/失败）
        if result is None:
            return {"cookie": None, "nickname": "", "douyin_id": "", "avatar": "",
                    "profile_dir": str(profile_dir) if tmp_path is not None else ""}
        # #fix-profile-dir-move：添加账号场景把临时 profile_dir 路径返给前端，
        # 前端调 /accounts 时一并传过来，后端 add_account 负责搬移到 accounts/{id}/profile/
        # （否则发布/搜索用 launch_persistent_context 会读到空 profile，无登录态）。
        # 重新登录场景 profile_dir 已是 accounts/{id}/profile/，不返（前端不需要处理）。
        if tmp_path is not None:
            result["profile_dir"] = str(profile_dir)
        return result
    finally:
        if tmp_path is not None:
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass


@router.get("", summary="账号分页列表")
def list_accounts(status: str = "", keyword: str = "", page: int = 1, page_size: int = 20):
    """账号分页列表（不含 Cookie）。"""
    return account_service.list_accounts(status, keyword, page, page_size)


@router.get("/available", summary="可选账号列表（选品拉取可用）")
def list_available_accounts():
    """返回 DB 里状态正常的账号列表。

    #fix-available-db-only：以 DB 记录为主，不再检测 accounts/<id>/profile/ + Cookies 文件目录。
    添加账号流程的 profile_dir 是临时路径（accounts.py:64 tempfile.mkdtemp），登录成功后
    未搬到账号目录，profile_dir 检测会把"刚刚登录成功的账号"误过滤掉。
    改为：deleted=0 AND status='normal' 即视为可用。
    """
    db = get_db()
    rows = db.query_all(
        "SELECT id, douyin_id, nickname, remark, status FROM account "
        "WHERE deleted=0 AND status='normal'"
    )
    out = [
        {
            "id": r["id"],
            "nickname": r["nickname"],
            "remark": r["remark"],
            "status": r["status"],
        }
        for r in rows
    ]
    return {"list": out, "total": len(out)}


@router.post("/{account_id}/relogin", summary="重新登录（登录窗抓 cookie + 页面身份）")
def relogin(account_id: str, req: ReloginRequest):
    """重新登录：用登录窗抓到的 cookie + profile 更新账号。"""
    profile = {
        "nickname": req.nickname,
        "douyin_id": req.douyin_id,
        "avatar": req.avatar,
    }
    try:
        return account_service.relogin(account_id, req.new_cookie, profile=profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{account_id}/check", summary="检测账号登录态")
def check_account_status(account_id: str):
    """手动触发单个账号的会话有效性检测，失效时返回 status='invalid'。
    检测逻辑与定时任务一致（调 account_service.check_account）。"""
    try:
        return account_service.check_account(account_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/{account_id}/remark", summary="编辑备注名")
def update_remark(account_id: str, req: UpdateRemarkRequest):
    """编辑备注名。"""
    try:
        account_service.update_remark(account_id, req.remark)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{account_id}/toggle-disable", summary="账号启停")
def toggle_disable(account_id: str, req: ToggleDisableRequest):
    """停用/恢复账号。"""
    try:
        account_service.toggle_disable(account_id, req.disabled)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/{account_id}", summary="删除账号")
def delete_account(account_id: str):
    """删除账号（二次确认由前端承担；后端逻辑删除+留痕通知）。"""
    try:
        account_service.delete_account(account_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
