# -*- coding: utf-8 -*-
"""账号模块请求/响应模型（F-01）。"""

from typing import Optional

from pydantic import BaseModel, Field


class AddAccountRequest(BaseModel):
    """添加账号（登录窗抓取模式，cookie + 页面身份信息）。"""
    cookie: str = Field(min_length=1, description="登录窗抓取的 Cookie 串")
    remark: str = Field(default="", description="备注名，默认取昵称")
    nickname: str = Field(default="", description="登录窗从页面 evaluate 拿到的昵称")
    douyin_id: str = Field(default="", description="登录窗从页面 evaluate 拿到的抖音号")
    avatar: str = Field(default="", description="登录窗从页面 evaluate 拿到的头像 URL")
    profile_dir: Optional[str] = Field(
        default=None,
        description="登录窗临时 chromium profile 目录（添加账号场景，cookie 落库后搬移到 accounts/{id}/profile/）",
    )


class ReloginRequest(BaseModel):
    """重新登录。"""
    new_cookie: str = Field(min_length=1, description="新 Cookie")
    nickname: str = Field(default="", description="登录窗从页面 evaluate 拿到的昵称")
    douyin_id: str = Field(default="", description="登录窗从页面 evaluate 拿到的抖音号")
    avatar: str = Field(default="", description="登录窗从页面 evaluate 拿到的头像 URL")


class UpdateRemarkRequest(BaseModel):
    """编辑备注名。"""
    remark: str = Field(min_length=0, max_length=50, description="新备注名")


class ToggleDisableRequest(BaseModel):
    """账号启停。"""
    disabled: bool = Field(description="True 停用 / False 恢复")


class LoginWindowRequest(BaseModel):
    """弹登录窗抓 Cookie（可选 account_id 用于重新登录时落盘 storage.json）。"""
    account_id: Optional[str] = Field(default=None, description="账号 ID（重新登录时传，登录成功后 storage.json 落到该账号目录）")
