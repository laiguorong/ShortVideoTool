"""抖音多账号会话管理模块。

#fix-unify-profile-storage：登录态走 chromium 持久化 profile 目录（accounts/<id>/profile/），
本模块只负责账号元数据管理（账号增删改查 + meta.json）。

存储目录结构：
    backend/data/accounts/<account_id>/
        ├── profile/      # chromium 持久化 profile（cookies + IndexedDB + localStorage）
        └── meta.json     # 账号元数据（昵称、头像、最后登录时间、备注）
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 账号数据根目录，与后端 data 目录同级
ACCOUNTS_ROOT = Path(__file__).resolve().parents[2] / "data" / "accounts"


def _resolve_accounts_root() -> Path:
    """解析账号目录：优先 get_data_dir() / accounts（与 DB 一致），失败时回退到 ACCOUNTS_ROOT。

    这样开发态（pychark backend）和 Electron 部署态（H:\\ShortVideoToolData）都能用。
    """
    try:
        from app.services.setting_service import get_data_dir
        return get_data_dir() / "accounts"
    except Exception:
        return ACCOUNTS_ROOT


def get_profile_dir(account_id: str) -> Path:
    """账号对应的 chromium 持久化 profile 目录（#452 替换 storage_state 方案）。

    与 storage.json 同目录 sibling：accounts/<id>/profile/。
    持久化 cookies + localStorage + IndexedDB + Service Worker + Cache，
    下次 launch_persistent_context(user_data_dir=...) 时直接复用。
    """
    from app.services.douyin_account import get_account_manager
    d = get_account_manager()._account_dir(account_id)
    profile = d / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile

# 抖音域名匹配（用于 storage_state 过滤与登录态校验）
DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "live.douyin.com",
    "creator.douyin.com",
    "www.iesdouyin.com",
}

# 视为"已登录"的 cookie 关键名
LOGIN_COOKIE_KEYS = {"ttwid", "sessionid", "uid", "user_unique_id"}


@dataclass
class AccountMeta:
    """账号元数据，存于 meta.json。"""

    account_id: str
    nickname: str = ""
    avatar_url: str = ""
    remark: str = ""
    created_at: float = field(default_factory=time.time)
    last_login_at: float = 0.0
    last_used_at: float = 0.0
    is_logged_in: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountMeta":
        # 兼容缺字段
        return cls(
            account_id=data.get("account_id") or str(uuid.uuid4()),
            nickname=data.get("nickname", ""),
            avatar_url=data.get("avatar_url", ""),
            remark=data.get("remark", ""),
            created_at=data.get("created_at", time.time()),
            last_login_at=data.get("last_login_at", 0.0),
            last_used_at=data.get("last_used_at", 0.0),
            is_logged_in=data.get("is_logged_in", False),
        )


@dataclass
class AccountSession:
    """账号会话运行态：浏览器 context + 关联元数据。"""

    meta: AccountMeta
    storage_path: Path  # storage.json 路径
    context: Any  # playwright BrowserContext，运行时由调用方注入
    page: Any = None  # 当前主页面


def is_douyin_cookie(cookie: dict[str, Any]) -> bool:
    """判断 cookie 是否属于抖音域名。"""
    domain = (cookie.get("domain") or "").lstrip(".")
    if not domain:
        return False
    return any(domain == h or domain.endswith("." + h) for h in DOUYIN_HOSTS)


class AccountManager:
    """账号管理器：负责账号目录的增删改查与会话切换。

    线程安全：所有写操作加锁（threading.RLock），适用于单进程多线程场景。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _resolve_accounts_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, AccountMeta] = {}
        self._reload_cache()

    # ---------- 缓存 ----------

    def _reload_cache(self) -> None:
        """扫描根目录，构建账号元数据缓存。"""
        with self._lock:
            self._cache.clear()
            for entry in self.root.iterdir():
                if not entry.is_dir():
                    continue
                meta_file = entry / "meta.json"
                if not meta_file.exists():
                    continue
                try:
                    data = json.loads(meta_file.read_text(encoding="utf-8"))
                    meta = AccountMeta.from_dict(data)
                    meta.account_id = entry.name  # 强制以目录名为准
                    self._cache[meta.account_id] = meta
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取账号元数据失败 %s: %s", meta_file, exc)

    # ---------- 目录 ----------

    def _account_dir(self, account_id: str) -> Path:
        """取账号目录，不存在则抛错。"""
        d = self.root / account_id
        if not d.exists():
            raise FileNotFoundError(f"账号不存在: {account_id}")
        return d

    def _meta_path(self, account_id: str) -> Path:
        return self._account_dir(account_id) / "meta.json"

    # ---------- 增删改查 ----------

    def list_accounts(self) -> list[AccountMeta]:
        """列出全部账号。"""
        with self._lock:
            self._reload_cache()
            return sorted(
                self._cache.values(),
                key=lambda m: m.last_used_at or m.created_at,
                reverse=True,
            )

    def get(self, account_id: str) -> Optional[AccountMeta]:
        with self._lock:
            return self._cache.get(account_id)

    def create_account(self, remark: str = "") -> AccountMeta:
        """新建账号占位（未登录状态）。返回元数据。"""
        with self._lock:
            account_id = str(uuid.uuid4())[:8]
            d = self.root / account_id
            d.mkdir(parents=True, exist_ok=True)
            meta = AccountMeta(
                account_id=account_id,
                remark=remark,
                created_at=time.time(),
            )
            (d / "meta.json").write_text(
                json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._cache[account_id] = meta
            logger.info("新建账号 %s", account_id)
            return meta

    def delete_account(self, account_id: str) -> bool:
        """删除账号目录（连同 profile 持久化目录一起）。"""
        with self._lock:
            d = self.root / account_id
            if not d.exists():
                return False
            shutil.rmtree(d)
            self._cache.pop(account_id, None)
            logger.info("删除账号 %s", account_id)
            return True


# ---------- 全局单例 ----------

_manager: Optional[AccountManager] = None
_manager_lock = threading.Lock()


def get_account_manager() -> AccountManager:
    """获取全局账号管理器单例。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AccountManager()
    return _manager