"""抖音浏览器会话封装。

把 Playwright 的 BrowserContext 生命周期包成单账号维度：
- 启动时为每个账号新建 BrowserContext，并注入 storage_state 实现"免扫码"。
- 退出/切号时持久化当前 storage_state 并关闭 context。

典型用法：

    from app.services.douyin_account import get_account_manager
    from app.services.douyin_session import DouyinSessionPool, BrowserKind

    pool = DouyinSessionPool(BrowserKind.CHROMIUM, headless=False)
    async with pool.use_account(account_id) as session:
        page = session.page
        await page.goto("https://www.douyin.com/")
        # 业务操作 ...
        await pool.save_current(account_id)   # 退出前落盘
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .douyin_account import (
    AccountManager,
    AccountMeta,
    extract_login_state,
    get_account_manager,
)

logger = logging.getLogger(__name__)


class BrowserKind(str, Enum):
    """浏览器内核。"""

    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


@dataclass
class SessionHandle:
    """运行中的账号会话句柄。"""

    account_id: str
    context: Any  # playwright BrowserContext
    page: Any  # playwright Page
    meta: AccountMeta
    opened_at: float = field(default_factory=lambda: __import__("time").time())


class DouyinSessionPool:
    """单进程会话池：管理多个 BrowserContext（每账号一个）。"""

    def __init__(
        self,
        browser_kind: BrowserKind = BrowserKind.CHROMIUM,
        headless: bool = False,
        manager: Optional[AccountManager] = None,
        user_data_root: Optional[Path] = None,
    ) -> None:
        self.browser_kind = browser_kind
        self.headless = headless
        self.manager = manager or get_account_manager()
        self._playwright = None
        self._browser = None
        self._sessions: dict[str, SessionHandle] = {}

    async def start(self) -> None:
        """懒启动 playwright 与浏览器进程。"""
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self.browser_kind.value)
        self._browser = await launcher.launch(headless=self.headless)
        logger.info("浏览器已启动: %s headless=%s", self.browser_kind, self.headless)

    async def stop(self) -> None:
        """关闭浏览器并清理所有 context。"""
        for handle in list(self._sessions.values()):
            try:
                await handle.context.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 context 异常: %s", exc)
        self._sessions.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("浏览器已停止")

    async def open_account(self, account_id: str) -> SessionHandle:
        """打开/切换到指定账号的会话。"""
        await self.start()
        if account_id in self._sessions:
            return self._sessions[account_id]

        meta = self.manager.get(account_id)
        if meta is None:
            raise FileNotFoundError(f"账号不存在: {account_id}")

        storage = self.manager.load_storage(account_id)
        context = await self._browser.new_context(storage_state=storage)
        page = await context.new_page()
        handle = SessionHandle(account_id=account_id, context=context, page=page, meta=meta)
        self._sessions[account_id] = handle
        meta.last_used_at = __import__("time").time()
        logger.info(
            "打开账号会话 %s cookies=%d origins=%d",
            account_id,
            len(storage.get("cookies", [])),
            len(storage.get("origins", [])),
        )
        return handle

    async def close_account(self, account_id: str, save: bool = True) -> dict[str, Any]:
        """关闭并可选落盘。"""
        handle = self._sessions.pop(account_id, None)
        if handle is None:
            return {"saved": False, "reason": "会话不存在"}
        try:
            if save:
                state = await handle.context.storage_state()
                self.manager.save_storage(account_id, state)
            await handle.context.close()
            return {"saved": save, "account_id": account_id}
        except Exception as exc:  # noqa: BLE001
            logger.error("关闭账号 %s 失败: %s", account_id, exc)
            return {"saved": False, "error": str(exc)}

    async def save_current(self, account_id: str) -> dict[str, Any]:
        """把当前 context 的 storage_state 立即落盘。"""
        handle = self._sessions.get(account_id)
        if handle is None:
            raise RuntimeError(f"账号会话未打开: {account_id}")
        state = await handle.context.storage_state()
        meta = self.manager.save_storage(account_id, state)
        return {
            "account_id": account_id,
            "logged_in": meta.is_logged_in,
            "saved_at": meta.last_login_at,
            **extract_login_state(state),
        }

    @asynccontextmanager
    async def use_account(self, account_id: str) -> AsyncIterator[SessionHandle]:
        """async with 形式的便捷封装。"""
        handle = await self.open_account(account_id)
        try:
            yield handle
        finally:
            # 不强制落盘，由调用方决定
            await self.close_account(account_id, save=False)

    def get_session(self, account_id: str) -> Optional[SessionHandle]:
        return self._sessions.get(account_id)

    def active_account_ids(self) -> list[str]:
        return list(self._sessions.keys())