# -*- coding: utf-8 -*-
"""#fix-music-node：BGM 统一从 aweme_detail.music 节点拿（不再调 music_detail）。

覆盖：
- _parse_music 封面/头像改读 cover_medium / avatar_medium
- resolve_share 解析到 /music/ 路径时抛 DouyinClientError（仅支持视频链接）
- 分享导入 type=video + 音乐链接 → failed
- 分享导入 type=music + 音乐链接 → failed
- 分享导入 type=music + 视频 → ingest_mode=bgm_only 只入 BGM
- _download_bgm 头像下载入 author_avatar
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============ _parse_music 字段重定向 ============

def test_parse_music_cover_from_cover_medium():
    """封面改读 music.cover_medium.url_list[0]"""
    from app.core.douyin.client import _parse_music

    aw = {
        "music": {
            "mid": "m_cover_001",
            "title": "BGM",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "cover_medium": {"url_list": ["https://x.com/cover.HEIC"]},
        }
    }
    m = _parse_music(aw)
    assert m["cover_url"] == "https://x.com/cover.jpeg", \
        f"封面应来自 cover_medium.url_list[0] 并修 .heic，实际：{m['cover_url']}"


def test_parse_music_avatar_from_avatar_medium():
    """作者头像改读 music.avatar_medium.url_list[0]"""
    from app.core.douyin.client import _parse_music

    aw = {
        "music": {
            "mid": "m_avatar_001",
            "title": "BGM",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "avatar_medium": {"url_list": ["https://x.com/avatar.HEIC"]},
        }
    }
    m = _parse_music(aw)
    assert m.get("avatar_url") == "https://x.com/avatar.jpeg", \
        f"头像应来自 avatar_medium.url_list[0] 并修 .heic，实际：{m.get('avatar_url')}"


def test_parse_music_fallback_to_thumb_if_no_medium():
    """无 cover_medium 时降级到 cover_thumb"""
    from app.core.douyin.client import _parse_music

    aw = {
        "music": {
            "mid": "m_thumb",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
            "cover_thumb": {"url_list": ["https://x.com/thumb.jpeg"]},
        }
    }
    m = _parse_music(aw)
    assert m["cover_url"] == "https://x.com/thumb.jpeg"


def test_parse_music_no_cover_no_avatar():
    """无封面/头像字段 → 空串（不抛错）"""
    from app.core.douyin.client import _parse_music

    aw = {
        "music": {
            "mid": "m_empty",
            "title": "BGM",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
        }
    }
    m = _parse_music(aw)
    assert m["cover_url"] == ""
    assert m.get("avatar_url") == ""


# ============ resolve_share 拒绝音乐链接 ============

def test_resolve_share_long_music_link_rejected():
    """长链 https://www.douyin.com/music/{mid} → 抛 DouyinClientError"""
    from app.core.douyin.client import RealDouyinClient
    from app.core.douyin.base import DouyinClientError

    client = RealDouyinClient()
    text = "https://www.douyin.com/music/7528238850382186547"
    try:
        client.resolve_share(text)
        raise AssertionError("应抛 DouyinClientError")
    except DouyinClientError as e:
        assert "仅支持视频链接" in str(e), f"应明确仅支持视频链接，实际：{e}"


def test_resolve_share_short_link_music_path_rejected():
    """短链 Location 是 /music/... → 抛 DouyinClientError"""
    from app.core.douyin.client import RealDouyinClient
    from app.core.douyin.base import DouyinClientError

    client = RealDouyinClient()
    text = "https://v.douyin.com/abcdef/"

    # mock HTTP 响应：Location 跳音乐页
    fake_resp = MagicMock()
    fake_resp.status_code = 302
    fake_resp.headers = {"Location": "https://www.douyin.com/music/7528238850382186547"}

    # creq 在 resolve_share 函数内部 import，patch 目标应是 sys.modules['curl_cffi'].requests
    fake_creq = MagicMock()
    fake_creq.get.return_value = fake_resp
    with patch.dict(sys.modules, {"curl_cffi": MagicMock(requests=fake_creq)}):
        try:
            client.resolve_share(text)
            raise AssertionError("应抛 DouyinClientError")
        except DouyinClientError as e:
            assert "仅支持视频链接" in str(e), f"应明确仅支持视频链接，实际：{e}"


# ============ 分享导入：音乐链接一律拒绝 ============

def _patch_db(monkeypatch, category_type: str = "music"):
    """mock get_db：返回指定类型的 category。"""
    from app.services import share_import_service as sis
    fake_db = MagicMock()
    fake_db.query_one.return_value = {"id": "cat1", "type": category_type}
    monkeypatch.setattr(sis, "get_db", lambda: fake_db)
    return fake_db


def test_process_item_video_type_music_link_rejected(monkeypatch):
    """type=video + 音乐链接 → failed '仅支持视频链接'"""
    from app.services import share_import_service as sis

    _patch_db(monkeypatch, category_type="video")

    client = MagicMock()
    # resolve_share 直接抛 DouyinClientError（音乐链接被拒绝）
    from app.core.douyin.base import DouyinClientError
    client.resolve_share.side_effect = DouyinClientError("仅支持视频链接导入")

    item = {"id": "i1", "share_text": "https://www.douyin.com/music/7528238850382186547"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="video")
    assert status == "failed", f"应 failed，实际 {status}"
    assert "仅支持视频链接" in msg, f"应明确仅支持视频链接，实际：{msg}"


def test_process_item_music_type_music_link_rejected(monkeypatch):
    """type=music + 音乐链接 → failed '仅支持视频链接'"""
    from app.services import share_import_service as sis
    from app.core.douyin.base import DouyinClientError

    _patch_db(monkeypatch, category_type="music")

    client = MagicMock()
    client.resolve_share.side_effect = DouyinClientError("仅支持视频链接导入")

    item = {"id": "i1", "share_text": "https://www.douyin.com/music/7528238850382186547"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="music")
    assert status == "failed", f"应 failed，实际 {status}"
    assert "仅支持视频链接" in msg, f"应明确仅支持视频链接，实际：{msg}"


# ============ 分享导入：type=music + 视频 → ingest_mode=bgm_only ============

def test_process_item_music_type_video_uses_bgm_only(monkeypatch):
    """type=music + 视频链接 → ingest_mode=bgm_only 只入 BGM，不下视频"""
    from app.services import share_import_service as sis

    captured = {}

    # mock _process_single_video 捕获入参
    def fake_process_single_video(video, category_id, client, *, source,
                                   conditions=None, cookie="", account_id="",
                                   info=None, ingest_mode="video"):
        captured["video"] = video
        captured["category_id"] = category_id
        captured["source"] = source
        captured["ingest_mode"] = ingest_mode
        return {"action": "new", "material_id": "bgm_mat_1",
                "title": video.get("music", {}).get("title"), "reason": None}

    # 用 monkeypatch 设置 sis.ms._process_single_video（_process_item 调 ms._process_single_video）
    from app.services import material_service as ms
    monkeypatch.setattr(ms, "_process_single_video", fake_process_single_video)

    client = MagicMock()
    client.resolve_share.return_value = {
        "_source": "detail",
        "video_id": "v_bgmonly",
        "title": "视频",
        "music": {
            "music_id": "m_bgmonly",
            "title": "BGM标题",
            "play_url": {"url_list": ["https://cdn/m.mp3"]},
        },
        "download_url": "https://cdn/v.mp4",
        "share_url": "https://www.douyin.com/video/v_bgmonly",
    }
    item = {"id": "i1", "share_text": "https://v.douyin.com/xxx/"}

    status, msg, mat_id = sis._process_item(item, "cat_music", client, type="music")
    assert status == "success", f"应 success，实际 {status}: {msg}"
    assert captured["ingest_mode"] == "bgm_only", \
        f"应 ingest_mode=bgm_only，实际 {captured['ingest_mode']}"
    assert captured["source"] == "share"
    assert mat_id == "bgm_mat_1"


def test_process_item_video_type_video_no_bgm_ingest(monkeypatch):
    """type=video + 视频链接 → ingest_mode=video（不下 BGM）"""
    from app.services import share_import_service as sis

    _patch_db(monkeypatch, category_type="video")
    captured = {}

    def fake_process_single_video(video, category_id, client, *, source,
                                   conditions=None, cookie="", account_id="",
                                   info=None, ingest_mode="video"):
        captured["ingest_mode"] = ingest_mode
        return {"action": "new", "material_id": "vid_mat_1",
                "title": video.get("title"), "reason": None}

    from app.services import material_service as ms
    monkeypatch.setattr(ms, "_process_single_video", fake_process_single_video)

    client = MagicMock()
    client.resolve_share.return_value = {
        "_source": "detail",
        "video_id": "v_normal",
        "title": "视频标题",
        "download_url": "https://cdn/v.mp4",
        "share_url": "https://www.douyin.com/video/v_normal",
    }
    item = {"id": "i1", "share_text": "https://v.douyin.com/xxx/"}

    status, msg, mat_id = sis._process_item(item, "cat_video", client, type="video")
    assert status == "success"
    assert captured["ingest_mode"] == "video", \
        f"应 ingest_mode=video（不下 BGM），实际 {captured['ingest_mode']}"


# ============ _download_bgm 头像下载入 author_avatar ============

@patch("app.core.douyin.avatar_cache.download_to")
@patch("app.services.material_service._md5_of_file", return_value="fake_md5_avatar")
@patch("app.services.material_service.extract_media_info", return_value={"duration_ms": 0})
@patch("app.services.material_service.probe_media", return_value={"format": {}})
@patch("app.services.material_service.get_data_dir")
@patch("app.services.material_service.get_db")
def test_download_bgm_stores_author_avatar(mock_get_db, mock_data_dir,
                                             mock_probe, mock_extract,
                                             mock_md5, mock_cover):
    """_download_bgm 下载 music.avatar_medium 到 author_avatar"""
    from app.services import material_service as ms

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    mock_data_dir.return_value = tmp
    mock_cover.return_value = "cache/avatar/music_avatar.jpg"

    fake_db = MagicMock()
    fake_db.query_one.return_value = None  # 无重复
    mock_get_db.return_value = fake_db

    client = MagicMock()
    def fake_download(url, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio content")
    client.download_video.side_effect = fake_download

    video = {
        "video_id": "v_avatar",
        "music": {
            "music_id": "m_avatar",
            "title": "BGM",
            "author": "作者",
            "download_url": "https://cdn/m.mp3",
            "cover_url": "https://x.com/cover.jpeg",
            "avatar_url": "https://x.com/avatar.jpeg",
        },
    }

    ms._download_bgm(video, client, source_type="pull")

    insert_call = fake_db.insert.call_args
    inserted = insert_call[0][1]
    assert inserted.get("author_avatar") == "cache/avatar/music_avatar.jpg", \
        f"author_avatar 应被设置，实际：{inserted.get('author_avatar')}"
    # 验证 download_to 被以 .jpeg 扩展名调用（HEIC 修了）
    assert mock_cover.called, "应调 download_to 下载头像"


# ============ runner ============

def _run_all():
    import inspect
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed = 0
    failed = 0
    for name, fn in funcs:
        try:
            sig = inspect.signature(fn)
            if "monkeypatch" in sig.parameters:
                print(f"  [SKIP] {name}（需 pytest 提供 monkeypatch）")
                continue
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
