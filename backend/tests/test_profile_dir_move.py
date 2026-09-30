# -*- coding: utf-8 -*-
"""添加账号 profile_dir 搬移单测。

修复场景：登录窗临时 profile_dir（tempfile.mkdtemp）未搬到 accounts/<id>/profile/，
导致后续 launch_persistent_context(user_data_dir=...) 启动空 profile → 无登录态
→ 发布失败。验证 _move_profile_dir 正确把临时目录内容搬到账号目录。
"""
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import account_service


def _make_dummy_profile(src_dir: Path) -> None:
    """构造一个伪 chromium profile 目录：含 Default/Cookies 文件 + 其他文件。"""
    default = src_dir / "Default" / "Network"
    default.mkdir(parents=True, exist_ok=True)
    (default / "Cookies").write_bytes(b"sqlite-cookies-bytes")
    (src_dir / "Local State").write_text("{}", encoding="utf-8")


def test_move_profile_dir_basic():
    """临时 profile_dir → accounts/{id}/profile/：内容完整搬移。"""
    # 临时源目录（含 Default/Network/Cookies 模拟文件）
    src = Path(tempfile.mkdtemp(prefix="login_profile_"))
    _make_dummy_profile(src)
    src_marker = (src / "Default" / "Network" / "Cookies").read_bytes()
    assert src_marker == b"sqlite-cookies-bytes"

    # 目标目录（mock get_profile_dir 返回此路径，避免污染真实 data 目录）
    dst = Path(tempfile.mkdtemp(prefix="account_profile_"))

    with patch("app.services.douyin_account.get_profile_dir", return_value=dst):
        account_service._move_profile_dir(str(src), "fake_account_id")

    # 1. 源目录已搬走（不存在）
    assert not src.exists(), f"源目录应已被搬走，实际仍存在: {src}"
    # 2. 目标目录存在
    assert dst.exists()
    # 3. 内容完整搬运
    cookies_path = dst / "Default" / "Network" / "Cookies"
    assert cookies_path.exists(), f"Cookies 文件应已搬过来: {cookies_path}"
    assert cookies_path.read_bytes() == b"sqlite-cookies-bytes"
    # 4. 其他文件也搬过来
    assert (dst / "Local State").exists()


def test_move_profile_dir_overwrites_existing_empty_dst():
    """目标目录已存在（get_profile_dir mkdir 出来的空目录）→ 删 + 搬，不变成 dst/src_basename/。"""
    src = Path(tempfile.mkdtemp(prefix="login_profile_"))
    _make_dummy_profile(src)

    dst = Path(tempfile.mkdtemp(prefix="account_profile_"))  # 已是空目录
    sentinel = dst / "old_sentinel"  # 在空目录里塞个标记，确认被覆盖
    sentinel.write_text("old", encoding="utf-8")

    with patch("app.services.douyin_account.get_profile_dir", return_value=dst):
        account_service._move_profile_dir(str(src), "fake_account_id")

    assert not src.exists()
    # dst 内容是 src 的（旧 sentinel 已被覆盖）
    assert not sentinel.exists(), "旧 dst 内容应被覆盖"
    assert (dst / "Default" / "Network" / "Cookies").exists()


def test_move_profile_dir_idempotent_when_same_path():
    """src == dst（重新登录场景 profile_dir 已是 accounts/{id}/profile/）→ 跳过。"""
    # 重新登录场景：profile_dir = get_profile_dir(account_id) → launch_persistent_context 直接写账号目录
    # add_account 收到的 src 就是 dst 本身 → 不应该 move
    path = Path(tempfile.mkdtemp(prefix="account_profile_"))
    _make_dummy_profile(path)

    with patch("app.services.douyin_account.get_profile_dir", return_value=path):
        # src 路径与 dst 相同（同一 Path 对象）
        account_service._move_profile_dir(str(path), "fake_account_id")

    # 仍存在，没被删（没有调用 shutil.rmtree）
    assert path.exists()
    assert (path / "Default" / "Network" / "Cookies").exists()


def test_move_profile_dir_missing_src_warns_no_crash():
    """src 不存在 → 记 warning，不抛异常（账号创建仍成功）。"""
    nonexistent = Path(tempfile.mkdtemp(prefix="login_profile_"))
    shutil.rmtree(nonexistent)  # 删掉模拟"源不存在"
    dst = Path(tempfile.mkdtemp(prefix="account_profile_"))

    with patch("app.services.douyin_account.get_profile_dir", return_value=dst):
        # 不抛异常
        account_service._move_profile_dir(str(nonexistent), "fake_account_id")

    # dst 未被创建 cookies（因为 src 没有）
    assert not (dst / "Default" / "Network" / "Cookies").exists()


def test_move_profile_dir_exception_warns_no_crash():
    """搬移过程抛异常 → 记 warning，不影响账号创建主流程。"""
    src = Path(tempfile.mkdtemp(prefix="login_profile_"))
    _make_dummy_profile(src)
    dst = Path(tempfile.mkdtemp(prefix="account_profile_"))

    # 模拟 shutil.move 抛 OSError
    with patch("app.services.douyin_account.get_profile_dir", return_value=dst), \
         patch("app.services.account_service.shutil.move",
               side_effect=OSError("disk full")):
        # 不抛
        account_service._move_profile_dir(str(src), "fake_account_id")

    # src 应保留（搬移失败时不动）
    assert src.exists()


# ============ runner ============

def _run_all():
    import inspect
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj) and inspect.isfunction(obj)
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