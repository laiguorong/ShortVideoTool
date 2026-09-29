# -*- coding: utf-8 -*-
"""头像/封面本地缓存：抖音 CDN URL 带签名（x-expires 数天过期），不能直接存库。

策略（#364 起）：
- 素材相关（video/music 的封面与作者头像）下载到 `material/<type>/<date>/<id>/` 下，
  与视频本体同目录；DB 存相对路径（如 `material/video/20260115/abc/abc_cover.webp`）。
- 账号头像（account_service）保留旧 key-based 路径 `cache/avatar/<key>.webp`，
  不在 #364 范围内。

同 key 文件已存在且非强制刷新时直接复用，不重复下载。
"""

from pathlib import Path
from urllib.request import Request, urlopen

from app.services.setting_service import get_data_dir

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

# URL 签名参数差异不影响文件名——用 URL 路径部分做键
_PATH_QUERY_SPLIT = "?"


def download_to(url: str, target_rel: str, force: bool = False) -> str:
    """#364：下载到指定相对 data 根的路径（调用方负责拼好路径模板）。

    参数:
        url: 远程 URL（带签名参数）
        target_rel: 目标相对路径（如 `material/video/20260115/abc/abc_cover.webp`）
        force: True 时已存在也重新下载
    返回:
        target_rel（成功）或空串（失败）
    """
    if not url or not target_rel:
        return ""
    abs_path = get_data_dir() / target_rel
    if abs_path.exists() and not force:
        return target_rel
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        req = Request(url, headers=_HEADERS)
        with urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return ""
            data = resp.read()
        if not data:
            return ""
        abs_path.write_bytes(data)
        return target_rel
    except Exception:  # noqa: BLE001 网络失败不阻塞主流程
        return ""


def cache_account_avatar(url: str, account_id: str, force: bool = False) -> str:
    """下载账号头像到 `accounts/<account_id>/avatar.jpeg`。

    与素材头像不同，账号头像放账号目录下（与 storage.json 同级），**长期保留**：
    - 不在 cache/ 下，启动清理（cleanup_startup_temp）不会删
    - 新下载成功 → 替换旧文件
    - 下载失败 → 保留旧文件（不删、不空），返回旧路径
    - 无 url / 无 account_id / 下载失败且无旧文件 → 返回空串

    返回相对 data 根的路径（如 `accounts/<id>/avatar.jpeg`）或空串。
    """
    if not url or not account_id:
        return ""
    target_rel = f"accounts/{account_id}/avatar.jpeg"
    abs_path = get_data_dir() / target_rel
    local = download_to(url, target_rel, force=force)
    if local:
        return local
    # 下载失败：旧文件在就继续用旧的（不删、不空）
    if abs_path.is_file():
        return target_rel
    return ""


# ---------- 旧 key-based 路径（账号头像用；保留兼容） ----------

def cache_avatar(url: str, key: str, force: bool = False) -> str:
    """下载账号头像到 `cache/avatar/<key>.jpeg`（不在 #364 范围内，账号独立保留）。

    # #头像漂移修复（2026-09-23）：抖音 CDN URL path ext 不稳定（`.image` / `.jpeg`
    # / `.webp` 都见过），导致同一 key 在 cache 目录里散落多份不同扩展名文件，
    # 同时 meta.json 与 DB account.avatar 指向不同文件 → 前端展示头像用错路径或 404。
    # 修复：固定扩展名 `.jpeg`（Chromium / Webkit / Firefox 全部兼容 image/jpeg，
    # 抖音 CDN 实际返回 image/jpeg 内容，所以即便存为 .jpeg 浏览器也能正确渲染）。
    """
    if not url or not key:
        return ""
    target_rel = f"cache/avatar/{key}.jpeg"
    return download_to(url, target_rel, force=force)


def cache_cover(url: str, key: str, force: bool = False) -> str:
    """旧封面缓存（仅 #364 迁移期间作为兜底使用，新代码应直接调 download_to）。"""
    if not url or not key:
        return ""
    path_part = url.split(_PATH_QUERY_SPLIT)[0]
    ext = Path(path_part).suffix or ".webp"
    if len(ext) > 6:
        ext = ".webp"
    target_rel = f"cache/cover/{key}{ext}"
    return download_to(url, target_rel, force=force)

