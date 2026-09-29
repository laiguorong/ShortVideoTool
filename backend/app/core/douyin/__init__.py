# -*- coding: utf-8 -*-
"""抖音客户端工厂：固定使用 real 客户端（#501 移除 mock 模式）。

real 客户端实现真实抖音接口（creator.douyin.com），需账号登录态。
"""

from app.core.douyin.base import DouyinClient, DouyinClientError, RiskControlError, LoginInvalidError
from app.core.douyin.search_api import SearchBlockedError

_client: DouyinClient | None = None


def get_douyin_client() -> DouyinClient:
    """获取 real 客户端单例（需账号 cookie 生效）。"""
    global _client
    if _client is None:
        from app.core.douyin.client import RealDouyinClient
        _client = RealDouyinClient()
    return _client


__all__ = [
    "DouyinClient", "DouyinClientError", "RiskControlError", "LoginInvalidError",
    "SearchBlockedError",
    "get_douyin_client",
]
