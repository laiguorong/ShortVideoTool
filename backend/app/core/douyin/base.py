# -*- coding: utf-8 -*-
"""抖音客户端抽象基类（真实实现 client.py，#501 移除 mock 模式）。

所有方法为同步签名（配合 task_service 线程模型）。
"""

from abc import ABC, abstractmethod
from typing import Optional


class DouyinClientError(Exception):
    """抖音客户端异常基类。"""


class RiskControlError(DouyinClientError):
    """风控拦截异常（触发冷却，见 rate_limiter）。"""


class LoginInvalidError(DouyinClientError):
    """登录态失效（Cookie 过期）。"""


class DouyinClient(ABC):
    """抖音平台客户端接口定义。

    统一约定：方法抛 RiskControlError 表示风控、LoginInvalidError 表示登录失效，
    其余异常按普通失败处理；返回值均为 dict。
    """

    @abstractmethod
    def check_cookie(self, cookie: str) -> dict:
        """校验 Cookie 有效性并返回账号信息。

        返回: {"valid": bool, "douyin_id": str, "nickname": str, "avatar": str}
        """

    @abstractmethod
    def search_videos(self, cookie: str, conditions: dict, page: int,
                      search_id: str = "") -> dict:
        """按条件搜索抖音视频（单页，素材拉取用）。

        参数:
            cookie: 登录态
            conditions: 搜索条件（keyword / sort_type / publish_range / duration_range）
            page: 页码（1 起，每页 10 条）
            search_id: 搜索会话 ID（同一轮翻页必须复用，空则由实现自行生成）
        返回: {"has_next": bool, "videos": [{"video_id", "title", "desc", "topics",
                 "share_url", "duration_ms", "orientation", "download_url"}]}
        """

    @abstractmethod
    def resolve_share(self, share_text: str) -> dict:
        """解析分享文本/短链，返回视频信息。

        返回: {"video_id": str, "title": str, "duration_ms": int,
                 "resolution": str, "download_url": str}
        异常: DouyinClientError 链接失效/风控/格式非法
        """

    @abstractmethod
    def download_video(self, url: str, save_path: str) -> str:
        """下载视频文件到指定路径，返回实际保存路径。"""

    @abstractmethod
    def publish_video(self, cookie: str, video_path: str, title: str, topics: str,
                      shop_poi_id: Optional[str],
                      account_id: Optional[str] = None,
                      schedule: str = "",
                      allow_save: Optional[bool] = None) -> dict:
        """发布视频（挂载门店）。

        参数:
            cookie: 登录态 Cookie 串（兼容旧调用）
            video_path: 视频绝对路径
            title: 作品简介（含话题 #xxx）
            topics: 话题串（备用，留空用 title 内 #xxx 解析）
            shop_poi_id: 门店 POI ID；非空时挂载地理标签
            account_id: 账号 ID（real 模式用其加载 storage_state；mock 可忽略）
            schedule: 定时发布时间 yyyy-MM-dd HH:mm[:ss]；空=立即发布
            allow_save: 允许他人保存视频 True/False；None=不动抖音默认

        返回: {"online_video_id": str, "success": bool, "message": str}
        """

