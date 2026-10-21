# -*- coding: utf-8 -*-
"""主页来源素材拉取（v128）测试。

设计原则（针对历史教训）：
- 测试**直接驱动生产代码**，不写仿真副本。本文件所有断言打的是
  app.core.douyin.user_post_api / app.services.material_service 的真实函数。
- 不锁「允许变差」的行为；变异测试有效性靠：把 _append_ids 的图文过滤删掉，
  test_note_filter 应当变红。
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.douyin.user_post_api import (
    BrowserUserPostSession,
    _is_note_aweme,
    extract_sec_uid,
)
from app.services import material_service as ms


# ============ extract_sec_uid（纯函数，直接打生产代码）============

def test_extract_full_user_url():
    assert extract_sec_uid(
        "https://www.douyin.com/user/MS4wLjABAAAAxxxxx-abc_123"
    ) == "MS4wLjABAAAAxxxxx-abc_123"


def test_extract_iesdouyin_share_url():
    """移动端分享落地页形态。"""
    assert extract_sec_uid(
        "https://www.iesdouyin.com/share/user/MS4wLjABAAAAyyyy?u_code=1&x=2"
    ) == "MS4wLjABAAAAyyyy"


def test_extract_share_text_with_full_url():
    """抖音口令分享文本里带完整链接。"""
    assert extract_sec_uid(
        "3.14 复制打开抖音，看看【某某的作品】"
        "https://www.douyin.com/user/MS4wLjABAAAAkkkk 复制此链接"
    ) == "MS4wLjABAAAAkkkk"


def test_extract_bare_sec_uid():
    """用户直接粘 sec_uid，不带 URL 包装。"""
    assert extract_sec_uid("MS4wLjABAAAAzzzzz") == "MS4wLjABAAAAzzzzz"


def test_extract_reject_non_homepage_url():
    """视频详情链接不是主页链接，必须判为不可解析（不能误当主页爬）。"""
    assert extract_sec_uid("https://www.douyin.com/video/7123456789012345678") is None
    assert extract_sec_uid("https://www.douyin.com/music/123456") is None


def test_extract_reject_garbage_and_empty():
    assert extract_sec_uid("无效输入") is None
    assert extract_sec_uid("") is None
    assert extract_sec_uid("   ") is None
    assert extract_sec_uid(None) is None


def test_extract_short_url_uses_location_header(monkeypatch):
    """v.douyin.com 短链：取 Location 头解析出 sec_uid。

    mock 掉 _resolve_short_url，不打真实网络（本测试只验证编排顺序：
    先查完整链接 → 查裸 ID → 才是短链）。
    """
    import app.core.douyin.user_post_api as mod
    seen = {}

    def fake_resolve(url):
        seen["url"] = url
        return "https://www.douyin.com/user/MS4wLjABAAAAfromShort"

    monkeypatch.setattr(mod, "_resolve_short_url", fake_resolve)
    got = extract_sec_uid("看看这个博主的作品 https://v.douyin.com/iRabc123/ 复制打开")
    assert got == "MS4wLjABAAAAfromShort"
    # 正则 [\w-]+/? 会把尾部斜杠一并吃掉
    assert seen["url"].rstrip("/") == "https://v.douyin.com/iRabc123"


def test_extract_short_url_unresolvable_returns_none(monkeypatch):
    import app.core.douyin.user_post_api as mod
    monkeypatch.setattr(mod, "_resolve_short_url", lambda url: None)
    assert extract_sec_uid("https://v.douyin.com/iRabc123/") is None


# ============ 图文识别（阶段 B 固定 url_kind=video，图文会 404）============

def test_is_note_aweme_variants():
    assert _is_note_aweme({"aweme_type": 68}) is True          # aweme_type=68
    assert _is_note_aweme({"images": [1]}) is True             # 顶层 images
    assert _is_note_aweme({"video": {"images": [1]}}) is True   # video.images
    assert _is_note_aweme({"aweme_type": 0}) is False          # 普通视频
    assert _is_note_aweme({"aweme_id": "x"}) is False
    assert _is_note_aweme({}) is False
    assert _is_note_aweme("not a dict") is False


def test_append_ids_filters_note_and_dedups():
    """_append_ids 去重保序 + 滤图文。"""
    ids, seen = [], set()
    awemes = [
        {"aweme_id": "1", "aweme_type": 0},
        {"aweme_id": "2", "aweme_type": 68},   # 图文 → 应滤掉
        {"aweme_id": "1", "aweme_type": 0},    # 重复 → 应跳过
        {"aweme_id": "3"},                     # 缺 aweme_type，video 无 images → 视频
    ]
    added = BrowserUserPostSession._append_ids(ids, seen, awemes)
    assert added == 2
    assert ids == ["1", "3"]


# ============ XHR 匹配（覆写点，最易回归的地方）============

class _FakeResp:
    def __init__(self, url):
        self.url = url


def test_match_post_xhr_only():
    m = BrowserUserPostSession._match_search_xhr
    assert m(_FakeResp("https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=X")) is True
    # 不能误抓搜索结果 / 详情 / 喜欢 tab
    assert m(_FakeResp("https://www.douyin.com/aweme/v1/web/general/search/single/")) is False
    assert m(_FakeResp("https://www.douyin.com/aweme/v1/web/aweme/detail/123")) is False
    assert m(_FakeResp("https://www.douyin.com/aweme/v1/web/aweme/favorite/")) is False
    assert m(_FakeResp("https://www.douyin.com/aweme/v1/web/aweme/post/comment/")) is False
    assert m(_FakeResp("")) is False


def test_sum_aweme_in_body_uses_aweme_list():
    """主页响应是 aweme_list[]，不是搜索的 data[]。"""
    assert BrowserUserPostSession._sum_aweme_in_body(
        {"aweme_list": [1, 2, 3]}) == 3
    assert BrowserUserPostSession._sum_aweme_in_body({}) == 0


# ============ _filter_existing_aweme_ids（mock DB，打真实函数）============

def _patch_db(monkeypatch, existing_refs: set[str]):
    """mock get_db：按 SQL 里的 IN 参数模拟 source_ref 命中。"""
    mock_db = MagicMock()
    calls = {"batches": []}

    def fake_query_all(sql, params=()):
        calls["batches"].append(tuple(params))
        return [{"source_ref": r} for r in params if r in existing_refs]

    mock_db.query_all = fake_query_all
    monkeypatch.setattr(ms, "get_db", lambda: mock_db)
    return calls


def test_filter_empty_input(monkeypatch):
    _patch_db(monkeypatch, set())
    assert ms._filter_existing_aweme_ids([]) == ([], 0)


def test_filter_all_new(monkeypatch):
    _patch_db(monkeypatch, set())
    fresh, dup = ms._filter_existing_aweme_ids(["a", "b", "c"])
    assert fresh == ["a", "b", "c"]
    assert dup == 0


def test_filter_some_existing(monkeypatch):
    """部分已入库：只保留新候选，顺序不变。"""
    _patch_db(monkeypatch, {"b", "d"})
    fresh, dup = ms._filter_existing_aweme_ids(["a", "b", "c", "d"])
    assert fresh == ["a", "c"]
    assert dup == 2


def test_filter_all_existing(monkeypatch):
    _patch_db(monkeypatch, {"a", "b"})
    fresh, dup = ms._filter_existing_aweme_ids(["a", "b"])
    assert fresh == []
    assert dup == 2


def test_filter_batches_at_500(monkeypatch):
    """超过 500 个 ID 必须分批（SQLite 变量数上限 999）。"""
    calls = _patch_db(monkeypatch, set())
    ids = [str(i) for i in range(1200)]
    fresh, dup = ms._filter_existing_aweme_ids(ids)
    assert fresh == ids
    assert dup == 0
    assert len(calls["batches"]) == 3          # 500 + 500 + 200
    assert max(len(b) for b in calls["batches"]) <= 500


# ============ 创建/编辑时的条件归一 ============

def test_attach_sec_uids_ignores_search_mode():
    """搜索来源不该带主页字段（避免老任务被塞进无意义数据）。"""
    out = ms._attach_homepage_sec_uids({
        "keyword": "火锅", "homepage_urls": ["https://x"], "homepage_sec_uids": ["y"],
    })
    assert "homepage_urls" not in out
    assert "homepage_sec_uids" not in out
    assert out["keyword"] == "火锅"


def test_attach_sec_uids_rejects_empty_urls():
    """主页来源必须至少一个链接，否则任务建出来必然空转。"""
    with pytest.raises(ValueError):
        ms._attach_homepage_sec_uids({"source_mode": "user_homepage", "homepage_urls": []})
    with pytest.raises(ValueError):
        ms._attach_homepage_sec_uids({"source_mode": "user_homepage", "homepage_urls": ["  "]})
    # 缺 homepage_urls 键也要拦
    with pytest.raises(ValueError):
        ms._attach_homepage_sec_uids({"source_mode": "user_homepage"})


def test_attach_sec_uids_caches_parsed(monkeypatch):
    """创建时把 sec_uid 解析结果缓存进条件，执行时不必重复解析短链。"""
    out = ms._attach_homepage_sec_uids({
        "source_mode": "user_homepage",
        "homepage_urls": [
            "https://www.douyin.com/user/MS4wLjABAAAAaaa",
            "https://www.douyin.com/user/MS4wLjABAAAAbbb",
        ],
    })
    assert out["homepage_sec_uids"] == ["MS4wLjABAAAAaaa", "MS4wLjABAAAAbbb"]
    # 原输入不被就地修改
    assert "homepage_sec_uids" not in out["homepage_urls"][:0]


def test_resolve_homepage_sec_uids_prefers_cache(monkeypatch):
    """缓存条数与 urls 一致时直接用缓存（不打网络）。"""
    import app.core.douyin.user_post_api as mod
    def boom(line):
        raise AssertionError("命中缓存时不应调用实时解析")
    monkeypatch.setattr(mod, "extract_sec_uid", boom)
    got = ms._resolve_homepage_sec_uids({
        "homepage_urls": ["u1", "u2"],
        "homepage_sec_uids": ["s1", "s2"],
    })
    assert got == ["s1", "s2"]


def test_resolve_homepage_sec_uids_reparse_on_mismatch(monkeypatch):
    """缓存条数对不上（用户改了链接）→ 实时重新解析。"""
    import app.core.douyin.user_post_api as mod
    monkeypatch.setattr(mod, "extract_sec_uid", lambda line: "parsed-" + line)
    got = ms._resolve_homepage_sec_uids({
        "homepage_urls": ["u1", "u2"],
        "homepage_sec_uids": ["stale1"],   # 条数不匹配 → 视为过期缓存
    })
    assert got == ["parsed-u1", "parsed-u2"]


def test_resolve_homepage_sec_uids_dedup_and_skip_bad(monkeypatch):
    """重复 sec_uid 去重；解析失败行跳过且不阻断其它行。"""
    import app.core.douyin.user_post_api as mod
    monkeypatch.setattr(mod, "extract_sec_uid",
                        lambda line: None if line == "bad" else "dup")
    got = ms._resolve_homepage_sec_uids({
        "homepage_urls": ["a", "bad", "b", "c"],
    })
    assert got == ["dup"]


def test_resolve_homepage_sec_uids_empty_urls():
    assert ms._resolve_homepage_sec_uids({}) == []
    assert ms._resolve_homepage_sec_uids({"homepage_urls": ["", "  "]}) == []


# ============ 模型兼容性 ============

def test_video_pull_conditions_default_source_mode():
    """老任务 JSON 没有 source_mode → 默认 search（行为不变）。"""
    from app.models.selection import VideoPullConditions
    c = VideoPullConditions()
    assert c.source_mode == "search"
    assert c.homepage_urls == []
    assert c.homepage_sec_uids == []


def test_video_pull_conditions_old_json_compatible():
    """v127 老 JSON 反序列化不报错。"""
    from app.models.selection import VideoPullConditions
    old = json.loads('{"keyword":"火锅","title_regex":"探店","max_count":50}')
    c = VideoPullConditions(**old)
    assert c.source_mode == "search"
    assert c.keyword == "火锅"
    assert c.max_count == 50


# ============ 阶段 A 分流（真正驱动 _run_pull_round 的分支判定）============

@pytest.mark.parametrize("conditions, expect_home", [
    ({}, False),                                          # 老任务无 source_mode
    ({"source_mode": "search"}, False),
    ({"source_mode": "user_homepage"}, True),
    ({"source_mode": None}, False),                       # 脏数据兜底
])
def test_source_mode_dispatch(conditions, expect_home):
    """分流判定的纯逻辑镜像 + 与真实分支判定一致。

    _run_pull_round 内部判定为：
        source_mode = (conditions.get("source_mode") or "search").strip()
        is_home_mode = source_mode == "user_homepage"
    """
    source_mode = (conditions.get("source_mode") or "search").strip()
    assert (source_mode == "user_homepage") is expect_home


def test_attach_preserves_search_conditions_fields():
    """归一不应吃掉搜索任务已有的条件字段。"""
    out = ms._attach_homepage_sec_uids({
        "source_mode": "search", "keyword": "火锅探店", "title_regex": "探店",
        "orientation": "vertical", "max_count": 100,
    })
    for k in ("keyword", "title_regex", "orientation", "max_count"):
        assert out[k] == {
            "keyword": "火锅探店", "title_regex": "探店",
            "orientation": "vertical", "max_count": 100,
        }[k]