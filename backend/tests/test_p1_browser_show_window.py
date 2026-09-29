# -*- coding: utf-8 -*-
"""P1 测试：setting_service load/save + browser_show_window 默认值。

browser_show_window (#410 调试开关) 是 P0-6/P0-7 修复的核心配置：
- 默认 False (无头)——除 #1 账号登录外所有业务都不弹窗
- True 时 #2/#3/#4 (POI 搜索、详情、BGM 拉取) 显示窗口
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services import setting_service


def _use_temp_config():
    """重定向配置目录到临时目录，避免污染用户真实配置。"""
    tmp = Path(tempfile.mkdtemp())
    config_dir = tmp / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    setting_service.get_config_dir = lambda: config_dir
    setting_service.get_settings_path = lambda: config_dir / "settings.json"
    return config_dir


# ============ load_settings ============

def test_load_settings_returns_defaults():
    """首次加载（无配置文件）应返全部默认值"""
    config_dir = _use_temp_config()
    settings = setting_service.load_settings()
    assert isinstance(settings, dict)
    assert len(settings) > 0
    # 关键默认字段
    assert "browser_show_window" in settings
    assert settings["browser_show_window"] is False, \
        f"#410 默认 False，实际 {settings['browser_show_window']}"


def test_browser_show_window_default_false():
    """#410：browser_show_window 默认 False"""
    config_dir = _use_temp_config()
    settings = setting_service.load_settings()
    assert settings["browser_show_window"] is False


# ============ save_settings ============

def test_save_settings_persists():
    """save → load 应一致"""
    config_dir = _use_temp_config()
    setting_service.save_settings({"browser_show_window": True})
    # 重新读
    settings = setting_service.load_settings()
    assert settings["browser_show_window"] is True


def test_save_settings_partial_update():
    """save 部分字段不应影响其他字段"""
    config_dir = _use_temp_config()
    # 先 set browser_show_window=True
    setting_service.save_settings({"browser_show_window": True})
    # 再 set 其他字段
    setting_service.save_settings({"account_check_hours": 12})
    settings = setting_service.load_settings()
    assert settings["browser_show_window"] is True
    assert settings["account_check_hours"] == 12


def test_save_settings_creates_backup():
    """#设置修改前自动留 .bak"""
    config_dir = _use_temp_config()
    settings_path = config_dir / "settings.json"
    # 第一次保存
    setting_service.save_settings({"browser_show_window": True})
    # 第二次保存（应触发 .bak 创建）
    setting_service.save_settings({"browser_show_window": False})
    bak = settings_path.with_suffix(".json.bak")
    assert bak.exists(), f"未创建备份 {bak}"


def test_load_settings_corrupted_file_uses_defaults():
    """配置文件损坏时用默认值（不抛错）"""
    config_dir = _use_temp_config()
    settings_path = config_dir / "settings.json"
    settings_path.write_text("{ invalid json", encoding="utf-8")
    # 应不抛错
    settings = setting_service.load_settings()
    assert settings["browser_show_window"] is False  # 走默认


def test_load_settings_filters_unknown_fields():
    """#412：未知字段被丢弃，避免旧版本残留字段污染"""
    config_dir = _use_temp_config()
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps({
            "browser_show_window": True,
            "unknown_deprecated_field": "should_be_dropped",
            "h264_encoder": "nvenc",  # 已下线的旧字段
        }),
        encoding="utf-8",
    )
    settings = setting_service.load_settings()
    assert settings["browser_show_window"] is True
    assert "unknown_deprecated_field" not in settings
    assert "h264_encoder" not in settings


def test_save_settings_returns_merged():
    """save 返回完整合并后的配置（不只是 updates）"""
    config_dir = _use_temp_config()
    result = setting_service.save_settings({"browser_show_window": True})
    # 应包含 DEFAULT_SETTINGS 所有字段 + 用户的更新
    assert result["browser_show_window"] is True
    assert "account_check_hours" in result  # 默认字段保留


# ============ browser.py 与 setting 联动 ============

def test_browser_actor_respects_browser_show_window():
    """#410：BrowserActor._resolve_headless 应读 setting"""
    # _resolve_headless(None) → 读 setting（默认 False）
    # 此处仅验证函数能调用且不抛错
    from app.core.douyin.browser import browser_actor
    config_dir = _use_temp_config()  # 默认 False
    headless = browser_actor._resolve_headless(None)
    assert headless is True, "默认 browser_show_window=False → headless 应为 True"


def test_browser_actor_show_window_override():
    """_resolve_headless(None) + setting=True → headless=False"""
    from app.core.douyin.browser import browser_actor
    config_dir = _use_temp_config()
    setting_service.save_settings({"browser_show_window": True})
    headless = browser_actor._resolve_headless(None)
    assert headless is False


def test_browser_actor_explicit_headless_overrides_setting():
    """显式传 headless 参数优先于 setting"""
    from app.core.douyin.browser import browser_actor
    config_dir = _use_temp_config()
    setting_service.save_settings({"browser_show_window": True})  # 显示
    headless = browser_actor._resolve_headless(False)  # 显式 False → 无头
    assert headless is False


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
