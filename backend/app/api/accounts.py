# -*- coding: utf-8 -*-
"""账号管理路由（F-01）。"""

import json

from fastapi import APIRouter, HTTPException

from app.models.account import (AddAccountRequest, ReloginRequest,
                                ToggleDisableRequest, UpdateRemarkRequest,
                                LoginWindowRequest)
from app.services import account_service

router = APIRouter(prefix="/accounts", tags=["账号管理"])


@router.post("", summary="添加账号（登录窗抓取的 Cookie + 页面身份信息）")
def add_account(req: AddAccountRequest):
    """添加账号：cookie + 页面 evaluate 拿到的 profile 一起落库。"""
    profile = {
        "nickname": req.nickname,
        "douyin_id": req.douyin_id,
        "avatar": req.avatar,
    }
    try:
        return account_service.add_account(req.cookie, req.remark, profile=profile)
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
        # 重新登录场景：profile_dir 持久化 + storage.json 备份
        from app.services.douyin_account import get_profile_dir
        from app.services.setting_service import get_data_dir
        profile_dir = get_profile_dir(account_id)
        # #506：原硬编码 H:\DouyinToolData 是 bug，改用 get_data_dir() 跟随配置
        # （H:\DouyinToolData 是早期测试期硬编码路径，作为 bug 历史保留以说明 PR #506 修复来源）
        save_path = get_data_dir() / "accounts" / account_id / "storage.json"  # 备份
        tmp_path = None
    else:
        # 添加账号场景：临时路径（账号还没建，等拿到 cookie 后才决定落哪）
        profile_dir = Path(tempfile.mkdtemp(prefix="login_profile_"))
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, prefix="login_")
        tmp.close()
        save_path = Path(tmp.name)
        tmp_path = save_path

    try:
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",  # 入口用根域；用户扫码后跳到子路径也可（域判定宽松）
            user_data_dir=profile_dir,
            save_path=str(save_path),
            timeout_s=300,
            headless=False,
        )
        # result: dict | None（None = 超时/失败）
        if result is None:
            return {"cookie": None, "nickname": "", "douyin_id": "", "avatar": ""}
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


@router.get("/available", summary="有 profile 的账号列表（选品拉取可选）")
def list_available_accounts():
    """返回本地 accounts 目录下有 profile/ 的账号 ID + 备注 + 状态（去 DB join）。

    #452：以 profile_dir 为准（持久化 chromium profile）；storage.json 备份作辅助。
    用途：选品创建任务时，前端从这里取账号，避免选了"账号记录存在但 profile 缺失"的孤儿账号。
    """
    from app.services.douyin_account import get_profile_dir
    from app.db import get_db
    db = get_db()
    rows = db.query_all("SELECT id, douyin_id, nickname, remark, status FROM account WHERE deleted=0")
    out = []
    for r in rows:
        profile = get_profile_dir(r["id"])
        # profile_dir 必须存在且有 Cookies（chromium 持久化标志）：
        # 新版 chromium 在 Default/Network/Cookies，旧版在 Default/Cookies，两处都查
        default_cookies = profile / "Default" / "Network" / "Cookies"
        legacy_cookies = profile / "Default" / "Cookies"
        if profile.exists() and (default_cookies.exists() or legacy_cookies.exists()):
            # 从 storage.json 读 cookie_count（备份）
            storage_path = profile.parent / "storage.json"
            try:
                cookies = json.loads(storage_path.read_text(encoding="utf-8")).get("cookies", [])
            except Exception:
                cookies = []
            out.append({
                "id": r["id"],
                "nickname": r["nickname"],
                "remark": r["remark"],
                "status": r["status"],
                "cookie_count": len(cookies),
            })
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
