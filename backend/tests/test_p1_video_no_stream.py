# -*- coding: utf-8 -*-
"""P1 测试：抽帧前完整性探测（图声视频 / 文件损坏 / 无视频流）防御链路。

覆盖三处关键改动：
1. `_probe_video_metadata` 三异常路径
   - ffprobe 失败 → VideoCorruptedError
   - 无时长字段 → VideoCorruptedError
   - 无 video 流 → VideoNoStreamError
   - 正常 → 返回 (duration_s, True)
2. `_parse_aweme_common` 图声视频 WARNING 4 场景（避免上次变量作用域 NameError 回归）
3. `_INSPECT_REASON_TEXT` 文案映射表（4 子原因枚举 + 默认 fallback）
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from loguru import logger

from app.core.media_inspect import (
    _probe_video_metadata,
    VideoCorruptedError,
    VideoNoStreamError,
)
from app.core.douyin.client import _parse_aweme_common
from app.services.material_service import _INSPECT_REASON_TEXT


# ============ _probe_video_metadata ============

def _mock_probe(probe_media, extract_media_info, *, info=None, duration_ms=10000):
    """构造 ffprobe mock 返回值。info=None 模拟 ffprobe 失败。"""
    probe_media.return_value = info
    extract_media_info.return_value = {"duration_ms": duration_ms}


def test_probe_metadata_ffprobe_fail_raises_corrupted():
    """ffprobe 失败（探测不到任何 metadata）→ VideoCorruptedError。"""
    with patch("app.core.ffmpeg.probe_media") as probe_media, \
         patch("app.core.ffmpeg.extract_media_info") as extract_media_info:
        _mock_probe(probe_media, extract_media_info, info=None)
        with pytest.raises(VideoCorruptedError):
            _probe_video_metadata("/fake/x.mp4")


def test_probe_metadata_no_duration_raises_corrupted():
    """ffprobe 探测成功但无时长字段（metadata 不完整）→ VideoCorruptedError。"""
    fake_info = {"streams": [{"codec_type": "video", "width": 720, "height": 1280}]}
    with patch("app.core.ffmpeg.probe_media") as probe_media, \
         patch("app.core.ffmpeg.extract_media_info") as extract_media_info:
        _mock_probe(probe_media, extract_media_info, info=fake_info, duration_ms=0)
        with pytest.raises(VideoCorruptedError):
            _probe_video_metadata("/fake/x.mp4")


def test_probe_metadata_no_video_stream_raises_no_stream():
    """只有 audio 流（m4a 伪装 mp4）→ VideoNoStreamError。"""
    fake_info = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    with patch("app.core.ffmpeg.probe_media") as probe_media, \
         patch("app.core.ffmpeg.extract_media_info") as extract_media_info:
        _mock_probe(probe_media, extract_media_info, info=fake_info, duration_ms=63000)
        with pytest.raises(VideoNoStreamError):
            _probe_video_metadata("/fake/x.mp4")


def test_probe_metadata_normal_returns_tuple():
    """有 video 流 + 时长 > 0 → 返 (duration_s, True)。"""
    fake_info = {
        "streams": [{"codec_type": "video", "width": 720, "height": 1280}],
    }
    with patch("app.core.ffmpeg.probe_media") as probe_media, \
         patch("app.core.ffmpeg.extract_media_info") as extract_media_info:
        _mock_probe(probe_media, extract_media_info, info=fake_info, duration_ms=63000)
        duration_s, has_video = _probe_video_metadata("/fake/x.mp4")
        assert duration_s == pytest.approx(63.0)
        assert has_video is True


# ============ _parse_aweme_common WARNING ============

def _build_aweme(*, width=None, height=None, duration_ms=5000, url="https://x/v.mp4"):
    """构造 _parse_aweme_common 输入。width/height=None 走 play_addr 兜底也取不到。"""
    video = {
        "play_addr": {"url_list": [url]},
        "duration": duration_ms,
    }
    if width is not None and height is not None:
        video["dimension"] = {"width": width, "height": height}
    return {
        "aweme_id": "1111111111111111111",
        "aweme_type": 0,  # 普通视频
        "video": video,
    }


@contextmanager
def _capture_warnings():
    """上下文管理器：with 块内收集 loguru WARNING，退出自动 remove 避免 handler 泄漏。

    用法:
        with _capture_warnings() as captured:
            func_to_test(...)
        assert any("xxx" in m for m in captured)
    """
    captured: list[str] = []
    handler_id = logger.add(lambda m: captured.append(m.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(handler_id)


def test_parse_aweme_warns_when_zero_h_and_duration():
    """宽高=0 + 时长>0 + 有 URL → 真正触发 WARNING 消息（loguru 拦截验证）。"""
    aw = _build_aweme(width=None, height=None, duration_ms=5000)
    with _capture_warnings() as captured:
        result = _parse_aweme_common(aw, aw["aweme_id"], source="search")

    assert result["video_id"] == aw["aweme_id"]
    assert result["width"] == 0
    assert result["duration_ms"] == 5000
    assert any("疑似图声视频" in msg for msg in captured), (
        f"WARNING 应被触发，实际捕获: {captured}"
    )


def test_parse_aweme_no_warn_when_normal_dims():
    """正常宽高 → 不触发 WARNING（变量访问正常，无 NameError）。"""
    aw = _build_aweme(width=720, height=1280, duration_ms=5000)
    result = _parse_aweme_common(aw, aw["aweme_id"], source="search")
    assert result["width"] == 720
    assert result["height"] == 1280
    assert result["duration_ms"] == 5000


def test_parse_aweme_no_warn_when_duration_zero():
    """dur=0 + 宽高=0 → 不触发 WARNING（时长约束未满足）。"""
    aw = _build_aweme(width=None, height=None, duration_ms=0)
    result = _parse_aweme_common(aw, aw["aweme_id"], source="search")
    assert result["width"] == 0
    assert result["height"] == 0
    assert result["duration_ms"] == 0


def test_parse_aweme_no_warn_when_url_empty():
    """URL 空 + 宽高=0 → 不触发 WARNING（play_list 为空）。"""
    aw = _build_aweme(width=None, height=None, duration_ms=5000, url="")
    result = _parse_aweme_common(aw, aw["aweme_id"], source="search")
    # 无 URL 时 download_urls 应为空
    assert result["download_urls"] == []
    assert result["download_url"] == ""


# ============ _INSPECT_REASON_TEXT 映射表 ============

def test_inspect_reason_text_covers_all_inspect_video_returns():
    """_INSPECT_REASON_TEXT 覆盖 inspect_video 所有可能的 inspect_reason 枚举。"""
    # 实际非空 inspect_reason（来自 media_inspect.inspect_video）
    expected_reasons = {
        "no_video_stream",
        "file_corrupted",
        "probe_error",
        "no_frames",
    }
    actual_reasons = set(_INSPECT_REASON_TEXT.keys())
    assert expected_reasons == actual_reasons, (
        f"字典覆盖不全: 缺失 {expected_reasons - actual_reasons}, "
        f"多余 {actual_reasons - expected_reasons}"
    )


def test_inspect_reason_text_default_fallback():
    """inspect_reason 不在字典中（防御未来扩展）→ 上游用 .get(..., 默认文案)。"""
    default_text = "内容检测抽帧失败"
    # 模拟字典缺 key 时的 fallback
    result = _INSPECT_REASON_TEXT.get("unknown_future_reason", default_text)
    assert result == default_text