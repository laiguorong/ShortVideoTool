# -*- coding: utf-8 -*-
"""/api/accounts/available 单测：以 DB 记录为主，不检测会话文件目录。

修复场景：添加账号流程的 profile_dir 是临时路径（accounts.py:64 tempfile.mkdtemp），
登录成功后未搬到 accounts/<id>/profile/，导致原"profile_dir 存在性"检测把刚登录成功的
账号误过滤。改为：deleted=0 AND status='normal' 即视为可用。
"""
import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import accounts as accounts_module


def _make_account_row(acc_id: str, douyin_id: str, nickname: str,
                      remark: str = "", status: str = "normal") -> dict:
    """构造 DB 返回的 account 行结构（list_available_accounts 用的字段）。"""
    return {
        "id": acc_id,
        "douyin_id": douyin_id,
        "nickname": nickname,
        "remark": remark,
        "status": status,
    }


def _patch_db(monkey_db_rows: list[dict], tmp_data_dir: Path):
    """mock get_db + get_data_dir（已绑定到 accounts 模块的引用）。

    query_all 用 side_effect 模拟 SQL WHERE 过滤（deleted=0 AND status='normal'），
    让测试能验证 list_available_accounts 的 SQL 过滤逻辑。
    """
    mock_db = MagicMock()

    def fake_query_all(sql, params=()):
        # 模拟真实 DB 的 WHERE deleted=0 AND status='normal' 过滤
        if "deleted=0 AND status='normal'" in sql:
            return [r for r in monkey_db_rows if r.get("status") == "normal"]
        return monkey_db_rows

    mock_db.query_all.side_effect = fake_query_all

    patches = [
        patch("app.api.accounts.get_db", return_value=mock_db),
        patch("app.api.accounts.get_data_dir", return_value=tmp_data_dir),
    ]
    for p in patches:
        p.start()
    return patches


def _stop_patches(patches):
    for p in patches:
        p.stop()


def test_list_available_accounts_returns_normal_only():
    """DB 里 normal/invalid/disabled/deleted 混合时，只返 deleted=0 AND status='normal'。"""
    tmp = Path(tempfile.mkdtemp())
    rows = [
        _make_account_row("acc1", "dy_1", "账号1", "账号1备注", "normal"),
        _make_account_row("acc2", "dy_2", "账号2", "账号2备注", "invalid"),  # 过滤
        _make_account_row("acc3", "dy_3", "账号3", "账号3备注", "disabled"),  # 过滤
        # deleted 由 SQL WHERE 过滤（不是 mock 责任）
    ]
    patches = _patch_db(rows, tmp)
    try:
        result = accounts_module.list_available_accounts()
    finally:
        _stop_patches(patches)

    assert result["total"] == 1, f"应只返 1 条 normal，实际 {result['total']}"
    assert result["list"][0]["id"] == "acc1"
    assert result["list"][0]["nickname"] == "账号1"


def test_list_available_accounts_does_not_require_profile_dir():
    """添加账号场景 profile_dir 是临时路径，从未搬到 accounts/<id>/profile/。
    不检测 profile_dir 存在性 → 该账号仍应返（统一以 DB 为主）。
    """
    tmp = Path(tempfile.mkdtemp())
    rows = [
        _make_account_row("acc_temp", "dy_temp", "刚加账号", "刚加备注", "normal"),
    ]
    patches = _patch_db(rows, tmp)
    try:
        result = accounts_module.list_available_accounts()
    finally:
        _stop_patches(patches)

    assert result["total"] == 1
    assert result["list"][0]["id"] == "acc_temp"
    # 不再返 cookie_count 字段（统一 profile_dir 后 storage.json 下线）
    assert "cookie_count" not in result["list"][0]


def test_list_available_accounts_no_storage_json_dependency():
    """storage.json 存在/损坏都不再影响 /accounts/available（统一 profile_dir 后）。"""
    tmp = Path(tempfile.mkdtemp())
    rows = [
        _make_account_row("acc_with_storage", "dy_x", "有 storage", "", "normal"),
    ]
    storage_dir = tmp / "accounts" / "acc_with_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "storage.json").write_text("{not valid json", encoding="utf-8")
    patches = _patch_db(rows, tmp)
    try:
        result = accounts_module.list_available_accounts()
    finally:
        _stop_patches(patches)

    assert result["total"] == 1
    assert result["list"][0]["id"] == "acc_with_storage"


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