# -*- coding: utf-8 -*-
"""#418：成品视频目录服务——选择目录/扫描合规视频/发布后归档移动。

约束：
- 路径只能通过 Electron dialog 选择（前端），后端不限制路径（用户本地任意目录）。
- 合规视频后缀：.mp4 / .mov / .avi / .mkv（白名单）。
- 发布成功后自动把视频移到目录下 _published/ 子目录；失败仅日志，不阻塞发布。
"""
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger

# 合规视频后缀白名单
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}

# 发布成功后归档子目录名（英文避免与中文文件名冲突）
PUBLISHED_SUBDIR = "_published"


def scan_video_dir(abs_dir_path: str) -> dict:
    """扫描并校验视频目录。

    参数:
        abs_dir_path: 绝对路径（前端 Electron dialog 拿到）
    返回:
        {
            "abs_path": "D:/Videos/12月",
            "dir_name": "12月",
            "video_count": 12,
            "video_files": ["D:/Videos/12月/v1.mp4", ...],
        }
    异常:
        ValueError: 路径不存在/不是目录/为空/无合规视频
    """
    p = Path(abs_dir_path)
    if not p.exists():
        raise ValueError(f"目录不存在：{abs_dir_path}")
    if not p.is_dir():
        raise ValueError(f"路径不是目录：{abs_dir_path}")

    all_files = [f for f in p.iterdir() if f.is_file()]
    valid_videos = sorted(
        [f for f in all_files if f.suffix.lower() in ALLOWED_VIDEO_EXTS]
    )

    if not valid_videos:
        if not all_files:
            raise ValueError(f"目录为空：{p.name}")
        raise ValueError(
            f"目录下 {len(all_files)} 个文件均不符合要求"
            f"（仅支持 .mp4/.mov/.avi/.mkv）：{p.name}"
        )

    return {
        "abs_path": str(p),
        "dir_name": p.name,
        "video_count": len(valid_videos),
        "video_files": [str(f) for f in valid_videos],
    }


def move_to_published(video_file_path: str) -> str:
    """发布成功后把视频移动到目录下 _published/ 子目录。

    策略：
    - 目标已存在同名 → 加时间戳后缀 v1-20260927-1430.mp4
    - 移动失败 → 仅 warn 日志，不抛异常（移动是辅助操作，不阻塞发布结果）
    - 返回新路径（成功）或原路径（失败）

    参数:
        video_file_path: 视频文件绝对路径
    返回:
        新绝对路径（成功移动）或原路径（移动失败）
    """
    try:
        p = Path(video_file_path)
        if not p.exists():
            logger.warning("[video_dir] 视频文件不存在，跳过移动: {}", video_file_path)
            return video_file_path
        # 已归档过的（_published 下）不再处理
        if p.parent.name == PUBLISHED_SUBDIR:
            return video_file_path
        published_dir = p.parent / PUBLISHED_SUBDIR
        published_dir.mkdir(exist_ok=True)
        target = published_dir / p.name
        if target.exists():
            timestamp = datetime.now().strftime("%Y%m%d-%H%M")
            target = published_dir / f"{p.stem}-{timestamp}{p.suffix}"
        shutil.move(str(p), str(target))
        logger.info("[video_dir] 移动已发布视频 {} → {}", p.name, target)
        return str(target)
    except Exception as exc:
        logger.warning("[video_dir] 文件移动失败（不阻塞发布）: {}: {}",
                       video_file_path, exc)
        return video_file_path


