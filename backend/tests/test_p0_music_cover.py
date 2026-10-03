# -*- coding: utf-8 -*-
"""#封面空白修复：音乐封面 URL .heic 大小写 + 路径漂移回归测试。

#fix-music-node：_music_cover_url / _parse_music_info 已删除（BGM 字段统一从
aweme_detail.music 节点读）。封面修复迁到 _normalize_image_url（被 _parse_music
调用处理 cover_medium / cover_thumb / avatar_medium 三处）。

涉及：
- backend/app/core/douyin/client.py `_normalize_image_url`
- backend/app/core/douyin/client.py `_parse_music`（封面/头像字段映射）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.douyin.client import _normalize_image_url, _parse_music


# ============ _normalize_image_url 行为锁定 ============

def test_normalize_empty():
    """空串原样返回（调用方判断 url 是否为空再决定要不要后续处理）"""
    assert _normalize_image_url("") == ""


def test_normalize_lowercase_heic():
    """小写 .heic → .jpeg（基础功能）"""
    assert _normalize_image_url(
        "https://p3-sign.douyinpic.com/aweme/100x100.jpeg~tplv.heic"
    ) == "https://p3-sign.douyinpic.com/aweme/100x100.jpeg~tplv.jpeg"


def test_normalize_uppercase_heic():
    """大写 .HEIC → .jpeg（#封面空白根因：原 .replace 大小写敏感）"""
    assert _normalize_image_url(
        "https://p3-sign.douyinpic.com/aweme/music.HEIC"
    ) == "https://p3-sign.douyinpic.com/aweme/music.jpeg"


def test_normalize_mixed_case_heic():
    """混合大小写 .Heic / .hEiC → .jpeg"""
    assert _normalize_image_url(
        "https://x.com/music.Heic"
    ) == "https://x.com/music.jpeg"
    assert _normalize_image_url(
        "https://x.com/music.hEiC"
    ) == "https://x.com/music.jpeg"


def test_normalize_keeps_query_param_heic():
    """query 参数里的 .heic 不动（避免误伤签名参数）"""
    assert _normalize_image_url(
        "https://x.com/music.jpeg?sign=abc.heic&t=123"
    ) == "https://x.com/music.jpeg?sign=abc.heic&t=123"


def test_normalize_path_with_query():
    """路径段有 .heic + query 参数：只改路径段"""
    assert _normalize_image_url(
        "https://x.com/music.HEIC?sign=abc&t=123"
    ) == "https://x.com/music.jpeg?sign=abc&t=123"


def test_normalize_passthrough_jpeg():
    """已经是 .jpeg 不动"""
    assert _normalize_image_url(
        "https://x.com/music.jpeg"
    ) == "https://x.com/music.jpeg"


def test_normalize_passthrough_webp():
    """.webp 不动（仅处理 .heic 漂移）"""
    assert _normalize_image_url(
        "https://x.com/music.webp"
    ) == "https://x.com/music.webp"


def test_normalize_no_extension():
    """无扩展名不动"""
    assert _normalize_image_url(
        "https://x.com/music?id=123"
    ) == "https://x.com/music?id=123"


def test_normalize_hash_fragment():
    """# 片段标识符前的 .heic 替换"""
    assert _normalize_image_url(
        "https://x.com/music.HEIC#anchor"
    ) == "https://x.com/music.jpeg#anchor"


# ============ _parse_music 封面/头像集成（#fix-music-node）============

def test_parse_music_cover_medium_heic_replaced():
    """#fix-music-node：music.cover_medium.url_list[0] 的 .HEIC 替换"""
    aw = {
        "music": {
            "mid": "m_cover_001",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "cover_medium": {"url_list": ["https://x.com/c.HEIC?sign=abc.heic"]},
        }
    }
    assert _parse_music(aw)["cover_url"] == "https://x.com/c.jpeg?sign=abc.heic"


def test_parse_music_avatar_medium_heic_replaced():
    """#fix-music-node：music.avatar_medium.url_list[0] 的 .HEIC 替换"""
    aw = {
        "music": {
            "mid": "m_avatar_001",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "avatar_medium": {"url_list": ["https://x.com/a.HEIC"]},
        }
    }
    assert _parse_music(aw)["avatar_url"] == "https://x.com/a.jpeg"


def test_parse_music_cover_jpeg_passthrough():
    """#fix-music-node：cover_medium 已经是 .jpeg 不动"""
    aw = {
        "music": {
            "mid": "m",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "cover_medium": {"url_list": ["https://x.com/m.jpeg"]},
        }
    }
    assert _parse_music(aw)["cover_url"] == "https://x.com/m.jpeg"


def test_parse_music_cover_empty_when_missing():
    """#fix-music-node：无 cover/avatar 字段 → 空串"""
    aw = {
        "music": {
            "mid": "m_empty",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
        }
    }
    m = _parse_music(aw)
    assert m["cover_url"] == ""
    assert m.get("avatar_url") == ""


def test_parse_music_returns_empty_when_no_play_url():
    """#fix-music-node：play_url 为空 → 整 dict 空（版权受限）"""
    aw = {
        "music": {
            "mid": "m_noplay",
            "cover_medium": {"url_list": ["https://x.com/c.jpeg"]},
            "avatar_medium": {"url_list": ["https://x.com/a.jpeg"]},
        }
    }
    # 版权受限曲目无下载直链 → 整段返回空（不入库）
    assert _parse_music(aw) == {}


# ============ runner ============

def _run_all():
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed = 0
    failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  [OK] {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed (total {len(funcs)})")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
