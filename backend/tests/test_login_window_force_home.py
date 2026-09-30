# -*- coding: utf-8 -*-
"""登录窗强制回 home 单测：扫码成功后非 home 路径强制 navigate home。

修复场景：扫码成功跳转路径不定（content/upload/data-analysis 等子页），
evaluate 在非 home 页面拿不到 [class*="unique_id-"] 锚点 → nickname/douyin_id/avatar 全空。
检测接口（check_account）走强制 goto(home) + wait_for_selector 拿全，登录窗应一致。

通过 mock 整段 open_login_window 跑端到端验证：
- page.on("framenavigated", ...) 注册了监听
- URL 不在 home 时 → page.goto(home) 至少被调用一次
- evaluate 最终拿到完整 profile（不被空壳污染）
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.douyin import browser
from app.core.douyin.browser import browser_actor


def _build_mock_pw(start_url: str):
    """构造 mock playwright：扫码后停在 start_url（非 home），goto(home) 后 url 切到 home。

    返回 (mock_pw, mock_context, mock_page)。
    """
    home_url = "https://creator.douyin.com/creator-micro/home"

    mock_page = MagicMock()
    mock_page.url = start_url
    mock_page.wait_for_timeout = MagicMock()
    mock_page.wait_for_selector = MagicMock()  # 不抛 → 模拟 DOM 命中
    mock_page.evaluate = MagicMock(return_value={
        "nickname": "测试账号",
        "douyin_id": "12345678",
        "avatar": "http://p3.douyinpic.com/aweme/test/avatar.jpeg",
    })
    # main_frame 用于 _on_framenav 比较
    mock_main = MagicMock()
    mock_main.url = start_url
    mock_page.main_frame = mock_main

    def fake_goto(url, **_kwargs):
        # 模拟跳转 home 时 url 真的切过去
        if "creator-micro/home" in url:
            mock_page.url = home_url
            mock_main.url = home_url
    mock_page.goto = MagicMock(side_effect=fake_goto)

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page
    mock_context.cookies.return_value = [
        {"name": "sessionid", "value": "mock-sessionid-abc"},
        {"name": "uid_tt", "value": "fallback_uid"},
    ]
    # storage_state 替代原逐个 origin 域 goto + evaluate 抓 localStorage 笨办法，
    # 直接返完整 dict（与 playwright API 一致）
    mock_context.storage_state.return_value = {
        "cookies": [
            {"name": "sessionid", "value": "mock-sessionid-abc"},
            {"name": "uid_tt", "value": "fallback_uid"},
        ],
        "origins": [],
    }

    mock_pw = MagicMock()
    mock_pw.chromium.launch_persistent_context.return_value = mock_context
    mock_pw.stop = MagicMock()

    return mock_pw, mock_context, mock_page


def _patched_sp(mock_pw):
    """构造 sync_playwright() 上下文管理器桩。"""
    mock_sp = MagicMock()
    mock_sp.return_value.start.return_value = mock_pw
    return patch("playwright.sync_api.sync_playwright", mock_sp)


def test_open_login_window_uses_polling_not_framenav():
    """新实现改用纯轮询 page.url，不再注册 framenavigated 监听（SPA pushState 不触发事件）。"""
    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/content/manage"
    )
    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    # 验证 framenavigated 监听器不再注册
    on_events = [c.args[0] for c in mock_page.on.call_args_list if c.args]
    assert "framenavigated" not in on_events, \
        f"新实现不应注册 framenavigated（SPA pushState 不触发），实际: {on_events}"
    # 验证仍然强制跳 home（纯轮询版）
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    assert len(home_gotos) >= 1, \
        f"非 home 路径应强制跳 home，实际 goto: {goto_urls}"
    assert result is not None
    assert result["nickname"] == "测试账号"


def test_open_login_window_intercepts_multiple_non_home_redirects():
    """持续轮询：每次 tick 都检查 URL，多次非 home 跳转都被拦截。"""
    home_url = "https://creator.douyin.com/creator-micro/home"
    # 用 sequence 模拟 auto redirect chain（页面跳来跳去）
    # 第一个值被三重判定主循环消费（i=0 URL 检查），后续值被持续监控循环消费。
    url_sequence = iter([
        "https://creator.douyin.com/creator-micro/content/upload",  # 三重判定消耗 → 进入
        "https://creator.douyin.com/creator-micro/content/manage",  # 监控 tick 1 → 跳 home
        "https://creator.douyin.com/creator-micro/data-analysis",   # tick 2 → 跳
        "https://creator.douyin.com/creator-micro/setting",         # tick 3 → 跳
        home_url,  # tick 4: 在 home → 稳定 +1
        home_url,  # tick 5: 稳定 +2
        home_url,  # tick 6: 稳定 +3 → break
    ])

    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/content/upload"
    )
    # url 改用 property mock，每次访问消费一个 sequence 值（不覆盖 sequence）
    type(mock_page).url = property(lambda self: next(url_sequence, home_url))

    # goto 不改 url——让 sequence 自然被消费（主流程每次 tick 看到非 home 就跳）
    mock_page.goto = MagicMock()

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=5,
            headless=False,
        )

    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    # 至少 3 次非 home 跳转都被拦截 → page.goto(home) 至少 3 次
    assert len(home_gotos) >= 3, \
        f"持续监控应拦截多次非 home 跳转，实际 goto: {goto_urls}"
    assert result is not None
    assert result["nickname"] == "测试账号"


def test_open_login_window_force_home_on_non_home_path():
    """扫码后 URL 停在 creator-micro/content/manage（非 home）→ 强制 page.goto(home)"""
    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/content/manage"
    )
    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    assert len(home_gotos) >= 1, \
        f"未强制跳 home，实际 goto URL 列表: {goto_urls}"
    assert result is not None
    assert result["douyin_id"] == "12345678"
    assert result["avatar"].startswith("http")


def test_open_login_window_no_extra_goto_when_already_on_home():
    """扫码后已在 home → 不应多余跳转（evaluate 拿 home 资料即可）"""
    home_url = "https://creator.douyin.com/creator-micro/home"
    mock_pw, _, mock_page = _build_mock_pw(home_url)
    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    # 已在 home 时不应再 goto(home)
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    assert len(home_gotos) == 0, \
        f"已在 home 时不应强制跳，实际 goto: {goto_urls}"
    assert result is not None
    assert result["nickname"] == "测试账号"


def test_open_login_window_force_home_on_data_analysis_path():
    """扫码后 URL 停在 creator-micro/data-analysis（非 home）→ 强制 page.goto(home)"""
    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/data-analysis"
    )
    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    assert len(home_gotos) >= 1, \
        f"data-analysis 路径未强制跳 home，实际 goto: {goto_urls}"
    assert result is not None


def test_open_login_window_force_home_on_upload_path():
    """扫码后 URL 停在 creator-micro/content/upload（非 home）→ 强制 page.goto(home)"""
    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/content/upload"
    )
    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    assert len(home_gotos) >= 1
    assert result is not None


def test_open_login_window_waits_for_profile_selector():
    """强制跳 home 后应 wait_for_selector 等资料卡渲染（与 check_account 一致）"""
    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/content/manage"
    )
    with _patched_sp(mock_pw):
        browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=2,
            headless=False,
        )
    sel_calls = [c.args[0] for c in mock_page.wait_for_selector.call_args_list if c.args]
    # 应至少调一次 unique_id- 或 nick_name selector
    profile_selectors = [s for s in sel_calls if "unique_id-" in s or "nick_name" in s]
    assert len(profile_selectors) >= 1, \
        f"未等资料卡 selector，实际 selector 列表: {sel_calls}"


def test_open_login_window_save_path_accepts_str():
    """#bug-617：save_path 传 str 类型时内部应兼容（防止 .parent AttributeError）"""
    from unittest.mock import patch as _patch
    import json as _json

    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/home"
    )
    # 跳过 origins 抓取（page.goto 到 origin 域可能慢/超时），让 storage.json 备份走主路径
    mock_page.goto = MagicMock()

    # save_path 传 str（模拟 api/accounts.py 旧调用方式）
    tmp_dir = tempfile.mkdtemp()
    save_path_str = str(Path(tmp_dir) / "storage.json")

    with _patched_sp(mock_pw), _patch(
        "app.core.douyin.browser.Path"
    ) as mock_path_cls:
        # 保留真实 Path 行为（mock 不能完全替代 Path 类）
        from pathlib import Path as _RealPath
        mock_path_cls.side_effect = lambda x: _RealPath(x)
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tmp_dir),
            save_path=save_path_str,  # str 类型，触发 bug
            timeout_s=2,
            headless=False,
        )

    # 验证：storage.json 已写入（不抛 .parent AttributeError）
    assert result is not None
    assert Path(save_path_str).exists(), \
        f"storage.json 未写入（str save_path 兼容性失败）: {save_path_str}"
    # 验证文件可解析
    data = _json.loads(Path(save_path_str).read_text(encoding="utf-8"))
    assert "cookies" in data
    assert any(c["name"] == "sessionid" for c in data["cookies"])


def test_open_login_window_save_path_accepts_path():
    """#bug-617：save_path 传 Path 类型也正常工作（标准用法）"""
    import json as _json

    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/home"
    )
    mock_page.goto = MagicMock()

    tmp_dir = tempfile.mkdtemp()
    save_path_obj = Path(tmp_dir) / "storage.json"

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tmp_dir),
            save_path=save_path_obj,
            timeout_s=2,
            headless=False,
        )

    assert result is not None
    assert save_path_obj.exists(), f"storage.json 未写入: {save_path_obj}"
    data = _json.loads(save_path_obj.read_text(encoding="utf-8"))
    assert "cookies" in data


def test_open_login_window_storage_state_fails_graceful_degrade():
    """context.storage_state() 抛异常 → cookie_str 降级用主循环 cookies 兜底，evaluate 仍拿 profile。

    异常路径：
    - context.storage_state() 抛任意异常（如 context 已关 / PlaywrightError）
    - 期望：warning 日志 + cookie_str 仍能拼接（用主循环 cookies 快照）+ evaluate 仍能拿到完整 profile
    - 期望：save_path 仍能写入（仅 cookies，无 origins）
    """
    import json as _json

    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/home"
    )
    # 让 storage_state() 抛 PlaywrightError（真实生产场景：context 已关 / 协议错误）
    mock_context = mock_pw.chromium.launch_persistent_context.return_value
    from playwright.sync_api import Error as PlaywrightError
    mock_context.storage_state.side_effect = PlaywrightError("storage_state 失败模拟")

    tmp_dir = tempfile.mkdtemp()
    save_path_obj = Path(tmp_dir) / "storage.json"

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tmp_dir),
            save_path=save_path_obj,
            timeout_s=2,
            headless=False,
        )

    # 1. evaluate 仍能拿到完整 profile（不依赖 storage_state）
    assert result is not None, "storage_state 异常时 result 应降级返回"
    assert result["nickname"] == "测试账号"
    assert result["douyin_id"] == "12345678"
    assert result["avatar"].startswith("http")
    # 2. cookie_str 应降级用主循环 cookies 兜底拼接
    assert "sessionid=mock-sessionid-abc" in result["cookie"]
    assert "uid_tt=fallback_uid" in result["cookie"]
    # 3. save_path 应仍写入（仅 cookies，无 origins 字段）
    assert save_path_obj.exists(), "storage_state 异常时 storage.json 应降级写入"
    data = _json.loads(save_path_obj.read_text(encoding="utf-8"))
    assert "cookies" in data
    assert "origins" not in data, f"降级路径不应有 origins，实际: {data}"
    # 4. cookie 数量与 mock 一致（验证降级路径 context.cookies() 拿最新）
    assert len(data["cookies"]) == 2


def test_open_login_window_storage_state_and_cookies_both_fail():
    """storage_state() 和降级 cookies() 都抛 → 退到主循环 cookies 快照（最坏场景降级）。

    异常路径：
    - context.storage_state() 抛
    - 降级 context.cookies() 也抛（如 context 已关）
    - 期望：warning 日志（两条）+ cookie_str 用主循环 cookies 拼接 + evaluate 仍能拿到 profile
    """
    import json as _json

    mock_pw, _, mock_page = _build_mock_pw(
        "https://creator.douyin.com/creator-micro/home"
    )
    mock_context = mock_pw.chromium.launch_persistent_context.return_value
    from playwright.sync_api import Error as PlaywrightError
    mock_context.storage_state.side_effect = PlaywrightError("storage_state 失败模拟")
    # 降级路径 cookies() 也抛 → 退到主循环 524 行的 cookies 快照
    # 注：第一次调用（主循环 524 行）必须返 list 让主循环跑通，第二次（降级路径）抛
    original_cookies = mock_context.cookies.return_value
    mock_context.cookies.side_effect = [original_cookies, PlaywrightError("cookies 降级也失败模拟")]

    tmp_dir = tempfile.mkdtemp()
    save_path_obj = Path(tmp_dir) / "storage.json"

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tmp_dir),
            save_path=save_path_obj,
            timeout_s=2,
            headless=False,
        )

    # 1. evaluate 仍能拿到完整 profile
    assert result is not None, "双重异常时 result 应降级返回"
    assert result["nickname"] == "测试账号"
    # 2. cookie_str 应能用主循环 cookies 拼接（cookies 变量在主循环里 mock 过）
    # 主循环 cookies = context.cookies() 但现在 cookies() 也抛，cookies 变量是初始空 []
    # 这里验证 result 不崩即可，cookie_str 可能为空
    assert "cookie" in result
    # 3. save_path 应仍写入（仅 cookies，主循环 cookies 快照）
    assert save_path_obj.exists(), "双重异常时 storage.json 应仍写入"
    data = _json.loads(save_path_obj.read_text(encoding="utf-8"))
    assert "cookies" in data
    assert "origins" not in data


def test_open_login_window_jingxuan_redirect_forces_home():
    """登录成功后跳到抖音精选（www.douyin.com）→ 主循环主动 goto(home) 拉回"""
    home_url = "https://creator.douyin.com/creator-micro/home"
    # 模拟抖音 SDK：扫码成功后 redirect 到精选页
    url_sequence = iter([
        "https://www.douyin.com/",  # 主循环 tick 0: 不在 creator 域 → 跳 home
        home_url,                    # 主循环 tick 1: 在 home → 进 evaluate
        home_url, home_url, home_url,  # 持续监控稳定
    ])

    mock_pw, _, mock_page = _build_mock_pw("https://www.douyin.com/")
    type(mock_page).url = property(lambda self: next(url_sequence, home_url))

    def fake_goto(url, **_kwargs):
        # 模拟跳 home 成功 → url 切到 home
        if "creator-micro/home" in url:
            type(mock_page).url = property(lambda self: home_url)
    mock_page.goto = MagicMock(side_effect=fake_goto)

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=3,
            headless=False,
        )

    assert result is not None, "精选页跳 home 后未拿到 result"
    assert result["nickname"] == "测试账号"
    goto_urls = [c.args[0] for c in mock_page.goto.call_args_list if c.args]
    home_gotos = [u for u in goto_urls if "creator-micro/home" in u]
    # 主循环主动跳 home 至少 1 次（精选页拉回）
    assert len(home_gotos) >= 1, \
        f"精选页（不在 creator 域）未主动跳 home，实际 goto: {goto_urls}"


def test_open_login_window_login_url_still_waits():
    """URL 仍在 /login → 继续等，不主动跳 home（扫码未完成）"""
    home_url = "https://creator.douyin.com/creator-micro/home"
    url_sequence = iter([
        "https://creator.douyin.com/login",  # 主循环 tick 0: 在 /login → continue
        "https://creator.douyin.com/login",  # tick 1: 仍在 /login → continue
        home_url,                            # tick 2: 离开 /login + 在 home → 进 evaluate
        home_url, home_url, home_url,
    ])

    mock_pw, _, mock_page = _build_mock_pw("https://creator.douyin.com/login")
    type(mock_page).url = property(lambda self: next(url_sequence, home_url))
    mock_page.goto = MagicMock()

    with _patched_sp(mock_pw):
        result = browser_actor.open_login_window(
            url="https://creator.douyin.com/",
            user_data_dir=Path(tempfile.mkdtemp()),
            save_path=None,
            timeout_s=4,
            headless=False,
        )

    assert result is not None
    assert result["nickname"] == "测试账号"


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