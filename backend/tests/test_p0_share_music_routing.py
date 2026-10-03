# -*- coding: utf-8 -*-
"""#fix-music-node：BGM 字段统一从 aweme_detail.music 节点读，分享导入只接视频链接。

回归测试覆盖：
- _process_item type=video + 视频 → ingest_mode="video"，入视频库
- _process_item type=music + 视频 → ingest_mode="bgm_only"，只入 BGM
- _process_item 音乐链接拒绝（type=video / type=music 都 rejected）
- _download_bgm 用 video.music 完整字段（封面/头像/作者）入音乐库
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_client(resolved_music: dict):
    """构造 mock client：resolve_share 返回带 video.music 节点的 aweme。"""
    client = MagicMock()
    client.resolve_share.return_value = {
        "video_id": "v123",
        "title": "视频标题",
        "share_url": "https://www.douyin.com/video/v123",
        # #fix-music-node：完整 music 节点（_parse_music 输出格式）
        "music": {
            "music_id": "m999",
            "title": "BGM标题",
            "author": "BGM作者",
            "duration_ms": 120000,
            "download_url": "https://cdn/m.mp3",
            "share_url": "https://www.douyin.com/music/m999",
            "cover_url": "https://x.com/cover_medium.jpeg",
            "avatar_url": "https://x.com/avatar.jpeg",
        },
        "download_url": "https://cdn/v.mp4",
        "_source": "detail",
    }
    return client


def _patch_db(monkeypatch, category_type: str = "music"):
    """mock get_db：返回 music category + 无重复素材。"""
    from app.services import share_import_service as sis
    fake_db = MagicMock()
    fake_db.query_one.side_effect = None
    fake_db.query_one.return_value = {"id": "cat1", "type": category_type}
    monkeypatch.setattr(sis, "get_db", lambda: fake_db)
    return fake_db


def _patch_process_single_video(monkeypatch):
    """mock _process_single_video 捕获入参。"""
    from app.services import share_import_service as sis
    from app.services import material_service as ms
    captured = {}

    def fake_process_single_video(video, category_id, client, *, source,
                                   conditions=None, cookie="", account_id="",
                                   info=None, ingest_mode="video"):
        captured["video"] = video
        captured["category_id"] = category_id
        captured["source"] = source
        captured["ingest_mode"] = ingest_mode
        return {
            "action": "new",
            "material_id": f"mat_{ingest_mode}_1",
            "title": video.get("title") or video.get("music", {}).get("title"),
            "reason": None,
        }

    monkeypatch.setattr(ms, "_process_single_video", fake_process_single_video)
    return captured


# ============ type=video + 视频链接 → ingest_mode="video" ============

def test_video_type_video_share_ingest_video(monkeypatch):
    """#fix-music-node：type=video + 视频链接 → ingest_mode=video，入视频库（不处理 BGM）。"""
    from app.services import share_import_service as sis

    _patch_db(monkeypatch, category_type="video")
    captured = _patch_process_single_video(monkeypatch)
    client = _make_client({})

    item = {"id": "i1", "share_text": "https://v.douyin.com/xxx/"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="video")

    assert status == "success", f"应 success，实际 {status}: {msg}"
    assert captured["ingest_mode"] == "video", \
        f"应 ingest_mode=video，实际 {captured['ingest_mode']}"
    assert captured["source"] == "share"
    assert mat_id == "mat_video_1"


# ============ type=music + 视频链接 → ingest_mode="bgm_only" ============

def test_music_type_video_share_ingest_bgm_only(monkeypatch):
    """#fix-music-node：type=music + 视频链接 → ingest_mode=bgm_only，只入 BGM 不下视频。"""
    from app.services import share_import_service as sis

    _patch_db(monkeypatch, category_type="music")
    captured = _patch_process_single_video(monkeypatch)
    client = _make_client({})

    item = {"id": "i1", "share_text": "https://v.douyin.com/xxx/"}
    status, msg, mat_id = sis._process_item(item, "cat_music", client, type="music")

    assert status == "success", f"应 success，实际 {status}: {msg}"
    assert captured["ingest_mode"] == "bgm_only", \
        f"应 ingest_mode=bgm_only，实际 {captured['ingest_mode']}"
    # 验证 video 是 resolved（含完整 music 节点）
    assert captured["video"]["music"]["music_id"] == "m999"


# ============ 音乐链接一律拒绝 ============

def test_video_type_music_link_rejected(monkeypatch):
    """#fix-music-node：type=video + 音乐链接 → failed '仅支持视频链接导入'"""
    from app.services import share_import_service as sis
    from app.core.douyin.base import DouyinClientError

    _patch_db(monkeypatch, category_type="video")
    client = MagicMock()
    client.resolve_share.side_effect = DouyinClientError(
        "仅支持视频链接导入，音乐链接请到视频页导入（自动抽取 BGM 入音乐库）")

    item = {"id": "i1", "share_text": "https://www.douyin.com/music/7528238850382186547"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="video")
    assert status == "failed", f"应 failed，实际 {status}"
    assert "仅支持视频链接" in msg, f"应明确仅支持视频链接，实际：{msg}"


def test_music_type_music_link_rejected(monkeypatch):
    """#fix-music-node：type=music + 音乐链接 → failed '仅支持视频链接导入'"""
    from app.services import share_import_service as sis
    from app.core.douyin.base import DouyinClientError

    _patch_db(monkeypatch, category_type="music")
    client = MagicMock()
    client.resolve_share.side_effect = DouyinClientError(
        "仅支持视频链接导入，音乐链接请到视频页导入（自动抽取 BGM 入音乐库）")

    item = {"id": "i1", "share_text": "https://www.douyin.com/music/7528238850382186547"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="music")
    assert status == "failed", f"应 failed，实际 {status}"
    assert "仅支持视频链接" in msg, f"应明确仅支持视频链接，实际：{msg}"


def test_short_link_music_path_rejected(monkeypatch):
    """#fix-music-node：短链 Location 跳音乐页 → failed '仅支持视频链接导入'"""
    from app.services import share_import_service as sis
    from app.core.douyin.base import DouyinClientError

    _patch_db(monkeypatch, category_type="music")
    client = MagicMock()
    client.resolve_share.side_effect = DouyinClientError(
        "仅支持视频链接导入，音乐链接请到视频页导入（自动抽取 BGM 入音乐库）")

    item = {"id": "i1", "share_text": "https://v.douyin.com/abcdef/"}
    status, msg, mat_id = sis._process_item(item, "cat1", client, type="music")
    assert status == "failed"
    assert "仅支持视频链接" in msg


# ============ _download_bgm 用 music 节点字段（#fix-music-node）============

def test_download_bgm_uses_music_node_fields(monkeypatch):
    """#fix-music-node：_download_bgm 直接用 video.music 节点字段，不再调 music_detail。"""
    from app.services import material_service as ms

    fake_db = MagicMock()
    fake_db.query_one.return_value = None  # 无重复
    monkeypatch.setattr(ms, "get_db", lambda: fake_db)

    client = MagicMock()
    def fake_download(url, path):
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio content")
    client.download_video.side_effect = fake_download

    # video.music 节点（_parse_music 输出格式）—— 完整字段
    video = {
        "video_id": "v_pull",
        "music": {
            "music_id": "m_pull_888",
            "title": "完整BGM",
            "author": "完整作者",
            "duration_ms": 60000,
            "download_url": "https://cdn/m.mp3",
            "share_url": "https://www.douyin.com/music/m_pull_888",
            "cover_url": "https://x.com/cover.jpeg",
            "avatar_url": "https://x.com/avatar.jpeg",
        },
    }

    with monkeypatch.context() as m:
        m.setattr("app.services.material_service.get_data_dir",
                  lambda: Path("/tmp/test_data"))
        m.setattr("app.services.material_service.probe_media",
                  lambda x: {"format": {}})
        m.setattr("app.services.material_service.extract_media_info",
                  lambda x: {"duration_ms": 60000})
        m.setattr("app.services.material_service._md5_of_file",
                  lambda x: "fake_md5")
        m.setattr("app.core.douyin.avatar_cache.download_to",
                  lambda url, path: "cache/avatar.jpg")
        ms._download_bgm(video, client, source_type="pull")

    insert_call = fake_db.insert.call_args
    inserted = insert_call[0][1]
    # 验证入 DB 用的是 music 节点的完整字段（不再是 music_detail 接口）
    assert inserted["title"] == "完整BGM"
    assert inserted["author_nickname"] == "完整作者"
    assert inserted["share_url"] == "https://www.douyin.com/music/m_pull_888"
    # #fix-music-node：作者头像入 author_avatar 字段
    assert inserted.get("author_avatar") == "cache/avatar.jpg"


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
