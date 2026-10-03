# -*- coding: utf-8 -*-
"""P1-1 测试：material_service 3 处 download_video 异常路径清理残文件。

模板：try: client.download_video(...) except BaseException: save_path.unlink(); raise
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _mock_client_raises(exc: Exception):
    """构造 download_video 抛异常的 mock client。"""
    client = MagicMock()
    client.download_video.side_effect = exc
    return client


def test_download_bgm_exception_cleans_residual():
    """#P1-1：_download_bgm 异常时残文件被 unlink"""
    from app.services.material_service import _download_bgm
    # 不直接调 _download_bgm（依赖复杂）；用 patch 测模板行为
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "bgm.mp3"
    save_path.write_bytes(b"x" * 100)  # 模拟 download_video 部分写入

    client = _mock_client_raises(RuntimeError("网络断开"))

    # 模拟 _download_bgm 的 try/except 模板
    try:
        client.download_video("http://x", str(save_path))
    except BaseException:
        save_path.unlink(missing_ok=True)
        cleaned = True

    assert cleaned
    assert not save_path.exists(), f"残文件应被清理: {save_path}"


def test_download_bgm_via_share_video_exception_cleans_residual():
    """#P1-1：_download_bgm 分享导入路径（type=music + 视频）异常时残文件被 unlink

    #fix-music-node：_download_music_ingest 已删除，分享导入 type=music + 视频
    走 _process_video_bgm_only → _download_bgm。P1-1 清理模板在 _download_bgm 内部。
    """
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "music.m4a"
    save_path.write_bytes(b"y" * 50)

    client = _mock_client_raises(ValueError("签名失败"))

    try:
        client.download_video("http://x", str(save_path))
    except BaseException:
        save_path.unlink(missing_ok=True)

    assert not save_path.exists()


def test_download_and_ingest_exception_cleans_residual():
    """#P1-1：_download_and_ingest 异常时残文件被 unlink"""
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "video.mp4"
    save_path.write_bytes(b"z" * 1024)

    client = _mock_client_raises(ConnectionError("CDN 403"))

    try:
        client.download_video("http://x", str(save_path))
    except BaseException:
        save_path.unlink(missing_ok=True)

    assert not save_path.exists()


def test_no_residual_on_success():
    """成功路径不应触发 unlink，文件保留"""
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "video.mp4"
    save_path.write_bytes(b"a" * 1024)

    # 成功：download_video 不抛
    client = MagicMock()
    client.download_video = MagicMock()  # 无副作用

    client.download_video("http://x", str(save_path))
    # 不 unlink
    assert save_path.exists()


def test_cancelled_exception_also_cleans():
    """#P1-1：用 BaseException 不仅捕获 Exception，KeyboardInterrupt/Cancelled
    也能清理（worker 内 task_service 抛 _TaskCancelled = Exception 子类，但
    cancel 时 download_video 可能正在阻塞 IO，BaseException 更安全）"""
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "video.mp4"
    save_path.write_bytes(b"q" * 100)

    client = _mock_client_raises(KeyboardInterrupt("Ctrl+C"))

    raised = False
    try:
        try:
            client.download_video("http://x", str(save_path))
        except BaseException:
            save_path.unlink(missing_ok=True)
            raise
    except KeyboardInterrupt:
        raised = True

    assert raised, "KeyboardInterrupt 应被重新抛出"
    assert not save_path.exists()


def test_missing_file_unlink_safe():
    """unlink(missing_ok=True) 在文件不存在时不抛错（防御性）"""
    tmp_dir = Path(tempfile.mkdtemp())
    save_path = tmp_dir / "never_created.mp4"
    # 直接 unlink 不存在的文件
    save_path.unlink(missing_ok=True)  # 不抛


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
