# -*- coding: utf-8 -*-
"""#fix-music-node：_download_bgm 入库回归测试。

#fix-music-node 重构后：
- _download_bgm 直接读 video.music 节点（_parse_music 输出），不再调 _fetch_music_detail
- 封面来自 music.cover_medium.url_list[0]
- 头像来自 music.avatar_medium.url_list[0]
- 同一首原声多视频共用 music.mid，按其去重
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_full_music_node(music_id: str) -> dict:
    """构造 _parse_music 输出的完整 music 节点（修复后新数据源）。"""
    return {
        "music_id": music_id,
        "title": f"BGM_{music_id}",
        "author": f"作者_{music_id}",
        "duration_ms": 120000,
        "download_url": f"https://cdn.example.com/{music_id}.mp3",
        "share_url": f"https://www.douyin.com/music/{music_id}",
        "cover_url": f"https://x.com/cover_{music_id}.jpeg",
        "avatar_url": f"https://x.com/avatar_{music_id}.jpeg",
    }


@patch("app.core.douyin.avatar_cache.download_to", return_value="cache/cover/fake.jpg")
@patch("app.services.material_service._md5_of_file", return_value="fake_md5_001")
@patch("app.services.material_service.extract_media_info", return_value={"duration_ms": 120000})
@patch("app.services.material_service.probe_media", return_value={"format": {}})
@patch("app.services.material_service.get_data_dir")
@patch("app.services.material_service.get_db")
def test_download_bgm_uses_music_node_fields(mock_get_db, mock_data_dir,
                                              mock_probe, mock_extract,
                                              mock_md5, mock_cover):
    """#fix-music-node：_download_bgm 直接用 video.music 节点字段入库。"""
    from app.services import material_service as ms

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    mock_data_dir.return_value = tmp

    fake_db = MagicMock()
    fake_db.query_one.return_value = None  # 无重复
    mock_get_db.return_value = fake_db

    client = MagicMock()
    def fake_download(url, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake audio content")
    client.download_video.side_effect = fake_download

    music_id = "m_pull_888"
    video = {
        "video_id": "v_pull_001",
        "music": _make_full_music_node(music_id),
    }

    ms._download_bgm(video, client, source_type="pull")

    insert_call = fake_db.insert.call_args
    inserted = insert_call[0][1]

    # 关键断言：入 DB 用的是 music 节点字段（不再是 music_detail 接口）
    assert inserted["title"] == f"BGM_{music_id}", \
        f"应入 music.title='BGM_{music_id}'，实际：{inserted['title']}"
    assert inserted["author_nickname"] == f"作者_{music_id}", \
        f"应入 music.author，实际：{inserted['author_nickname']}"
    assert inserted["share_url"] == f"https://www.douyin.com/music/{music_id}"
    # #fix-music-node：作者头像入 author_avatar
    assert inserted.get("author_avatar") == "cache/cover/fake.jpg", \
        f"author_avatar 应被设置，实际：{inserted.get('author_avatar')}"
    # 不应再调 _fetch_music_detail
    assert not client._fetch_music_detail.called, \
        "#fix-music-node 后 _download_bgm 不再调 _fetch_music_detail"


@patch("app.services.material_service.get_db")
def test_download_bgm_skips_when_no_music(mock_get_db):
    """#fix-music-node：无 music 节点 → 跳过"""
    from app.services import material_service as ms

    client = MagicMock()
    video = {"video_id": "v123", "music": {}}  # 空 music 节点
    ms._download_bgm(video, client, source_type="pull")

    # 不应调 _fetch_music_detail
    assert not client._fetch_music_detail.called
    mock_get_db.return_value.insert.assert_not_called()


@patch("app.services.material_service.get_db")
def test_download_bgm_skips_when_no_download_url(mock_get_db):
    """#fix-music-node：music 节点 download_url 为空 → 跳过（版权受限）"""
    from app.services import material_service as ms

    client = MagicMock()
    video = {
        "video_id": "v123",
        "music": {
            "music_id": "m_copyrighted",
            "title": "版权受限",
            "download_url": "",  # 关键：空
            "cover_url": "",
            "avatar_url": "",
        },
    }
    ms._download_bgm(video, client, source_type="pull")

    # 不应 insert
    mock_get_db.return_value.insert.assert_not_called()


@patch("app.core.douyin.avatar_cache.download_to", return_value="cache/cover/fake.jpg")
@patch("app.services.material_service._md5_of_file", return_value="fake_md5_dup")
@patch("app.services.material_service.extract_media_info", return_value={"duration_ms": 0})
@patch("app.services.material_service.probe_media", return_value={"format": {}})
@patch("app.services.material_service.get_data_dir")
@patch("app.services.material_service.get_db")
def test_download_bgm_dedup_by_music_id(mock_get_db, mock_data_dir,
                                         mock_probe, mock_extract,
                                         mock_md5, mock_cover):
    """#fix-music-node：同一 music_id 已入库 → 跳过（不重复入库）"""
    from app.services import material_service as ms

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    mock_data_dir.return_value = tmp

    fake_db = MagicMock()
    # 第一次查 source_ref 命中（已有同 music_id 记录）→ 含 title 用于 reason 拼接
    fake_db.query_one.return_value = {"id": "existing_mat", "title": "已有BGM"}
    mock_get_db.return_value = fake_db

    client = MagicMock()
    music_id = "m_dup_001"
    video = {
        "video_id": "v_dup",
        "music": _make_full_music_node(music_id),
    }

    ms._download_bgm(video, client, source_type="pull")

    # 不应 insert（去重跳过）
    fake_db.insert.assert_not_called()
    # 不应下载（去重跳过前）
    client.download_video.assert_not_called()


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
            if any(p.default is not inspect.Parameter.empty for p in sig.parameters.values()):
                print(f"  [SKIP] {name}（需 @patch 装饰器）")
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
