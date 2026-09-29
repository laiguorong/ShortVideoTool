# -*- coding: utf-8 -*-
"""本地文件服务：头像/素材/片段/成品等 DB 相对路径资源经此接口读取。

安全：仅放行白名单子目录，路径解析后必须仍在对应白名单目录下（防目录穿越）。

Range（#82）：Starlette 0.38 的 FileResponse 不支持 Range 请求，浏览器 <video>
拿不到 206 部分响应，成品视频既无法起播也无法拖动进度条，故此处自行实现。
"""

import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.services.setting_service import get_data_dir

router = APIRouter(prefix="/files", tags=["文件服务"])

# 允许访问的子目录白名单（缓存 + 素材预览 + 项目片段/封面 + 成品视频）
# 路径前缀匹配，带分隔符边界（避免 cache 前缀误匹配 cache_evil）
# #363：项目片段路径 project/<pid>/clip/* 与封面 project/<pid>/cover/* 都在 "project" 前缀下
_ALLOWED_PREFIXES = ("cache", "material", "project", "finished", "accounts")

# accounts 目录含敏感文件（storage.json/sessionid、meta.json），
# 仅允许访问头像文件，其余拒绝（#504 头像迁移到账号目录后补的安全校验）
import re as _re
_ACCOUNT_AVATAR_RE = _re.compile(r"^accounts/[^/]+/avatar\.jpeg$")

# 流式响应分块大小（256KB）：大视频按块读出，避免整文件进内存
_CHUNK_SIZE = 256 * 1024


def _allowed(rel_path: str) -> bool:
    """相对路径是否落在白名单前缀内（含分隔符边界）。

    accounts 前缀特殊处理：仅允许 accounts/<id>/avatar.jpeg，其余（storage.json 等）拒绝。
    """
    if rel_path == "accounts" or rel_path.startswith("accounts/"):
        return bool(_ACCOUNT_AVATAR_RE.match(rel_path))
    return any(rel_path == p or rel_path.startswith(p + "/") for p in _ALLOWED_PREFIXES)


def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    """解析单段 Range 请求头，返回闭区间 (start, end)；无法识别时返回 None。

    支持的三种形式：
        bytes=100-200  指定起止
        bytes=100-     从 100 到文件末尾
        bytes=-500     最后 500 字节
    多段（含逗号）或非法值一律返回 None，由调用方退化为整文件响应。
    """
    if not range_header or not range_header.startswith("bytes=") or "," in range_header:
        return None
    spec = range_header[len("bytes="):].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            # 后缀形式 bytes=-N：取最后 N 字节
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start, end = max(0, size - suffix), size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    # 越界或空区间视为非法（size 为 0 时也会走到这里）
    if start < 0 or start > end or start >= size:
        return None
    return start, end


def _stream_file(file_path: Path, start: int, end: int):
    """按字节闭区间流式产出文件内容。"""
    remaining = end - start + 1
    with open(file_path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/{rel_path:path}", summary="读取缓存/素材/片段/成品文件")
def get_file(rel_path: str, request: Request):
    """按 data 根相对路径返回文件（白名单：cache/、material/、create/clip/、finished/）。"""
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    if not _allowed(rel_path):
        raise HTTPException(status_code=403, detail="路径不在允许范围内")
    data_root = get_data_dir()
    file_path = (data_root / rel_path).resolve()
    # 二次校验：解析后必须仍在白名单目录内（防 ../ 穿越）
    roots = [(data_root / p).resolve() for p in _ALLOWED_PREFIXES]
    if not any(file_path == r or str(file_path).startswith(str(r) + os.sep) for r in roots):
        raise HTTPException(status_code=403, detail="非法路径")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    size = file_path.stat().st_size
    rng = _parse_range(request.headers.get("range", ""), size)
    if rng is None:
        # 无 Range 或 Range 非法：整文件响应，并声明支持 Range 供播放器后续拖动
        return FileResponse(file_path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})
    start, end = rng
    # #504：Range 响应——用 StreamingResponse + Content-Length + Content-Range。
    # _stream_file 严格按闭区间读到 remaining=0，确保 yield 字节数 == Content-Length，
    # 避免大文件流式途中断导致 ERR_CONTENT_LENGTH_MISMATCH。
    return StreamingResponse(
        _stream_file(file_path, start, end), status_code=206, media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        })
