# -*- coding: utf-8 -*-
"""P0 端到端 import 校验：所有修改过的模块能被 import 且无副作用错误。

不依赖外部资源（playwright / DB / 文件），仅验证模块加载。
"""
import sys
import importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


MODULES = [
    # 核心修改
    "app.core.douyin.browser",
    "app.core.douyin.client",
    # service 修改
    "app.services.task_service",
    "app.services.account_service",
    "app.services.publish_service",
    "app.services.creation_service",
    "app.services.material_service",
    "app.services.selection_service",
    "app.services.share_import_service",
    "app.services.upload_service",
    # 间接依赖
    "app.services.setting_service",
    "app.services.douyin_account",
    "app.db.database",
]


def test_module_imports_clean():
    """每个模块应能无错 import（不抛 ImportError / AttributeError）"""
    failed: list[str] = []
    for mod_name in MODULES:
        try:
            importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{mod_name}: {type(e).__name__}: {e}")
    if failed:
        raise AssertionError("import 失败:\n" + "\n".join(failed))


def test_interruptible_sleep_exported():
    """#P1-3：interruptible_sleep 应从 task_service 导出"""
    from app.services.task_service import interruptible_sleep
    assert callable(interruptible_sleep)


def test_task_service_default_message_keys():
    """#P0-10：_DEFAULT_MESSAGE 应有 4 个终态 key"""
    from app.services.task_service import _DEFAULT_MESSAGE
    for k in ("success", "partial", "failed", "cancelled"):
        assert k in _DEFAULT_MESSAGE, f"缺失 {k}"
    assert _DEFAULT_MESSAGE["partial"] == "部分成功"
    assert _DEFAULT_MESSAGE["failed"] == "失败"
    assert _DEFAULT_MESSAGE["cancelled"] == "已取消"


def test_publish_service_uses_interruptible_sleep():
    """#P1-3：publish_service 应已 import interruptible_sleep（不再用 time.sleep）"""
    import inspect
    from app.services import publish_service
    src = inspect.getsource(publish_service)
    assert "interruptible_sleep" in src, "publish_service 未使用 interruptible_sleep"
    # 重试循环内不应再有 time.sleep(...) 调用
    import re
    time_sleep_calls = re.findall(r"time\.sleep\(", src)
    assert len(time_sleep_calls) == 0, f"publish_service 仍有 {len(time_sleep_calls)} 处 time.sleep 调用"


def test_material_service_has_cleanup_pattern():
    """#P1-1：material_service 3 处 download_video 都有清理模式"""
    import inspect
    from app.services import material_service
    src = inspect.getsource(material_service)
    # 检查至少 3 处 unlink(missing_ok=True) 在 download 失败路径
    count = src.count("except BaseException:\n        # #P1-1")
    assert count >= 3, f"应有 ≥3 处 P1-1 清理模板，实际 {count}"


def test_account_service_profile_blank_guard():
    """#P0-9：account_service 应有 prof_blank 守卫"""
    import inspect
    from app.services import account_service
    src = inspect.getsource(account_service)
    assert "prof_blank" in src, "缺少 prof_blank 守卫"
    assert "and \"avatar\" not in fields" in src, "未修 or→and 语义"


def test_creation_service_returns_contract():
    """#P0-10：creation_service 不再返字符串"""
    import inspect
    from app.services import creation_service
    src = inspect.getsource(creation_service)
    # 旧代码有 return "FFmpeg 不可用"，应被替换
    assert "return \"FFmpeg 不可用\"" not in src
    # 新代码 worker 异常应返 failed
    assert "info.message = \"FFmpeg 不可用\"" in src
    assert "return \"failed\"" in src


def test_upload_service_summary_from_db_exists():
    """#P1-5：upload_service 应有 _update_task_summary_from_db 函数"""
    from app.services.upload_service import _update_task_summary_from_db
    assert callable(_update_task_summary_from_db)


def test_browser_actor_open_login_uses_independent_pw():
    """#P0-6：open_login_window 应使用独立 sync_playwright 实例"""
    import inspect
    from app.core.douyin import browser
    src = inspect.getsource(browser.BrowserActor.open_login_window)
    # 新代码用独立 sync_playwright
    assert "sync_playwright().start()" in src
    # 不应再直接用 self._tls.browser 创建 context（独立 browser）
    assert "self._tls.browser.new_context" not in src
    # 函数体内不应有 with self._run_lock
    assert "with self._run_lock" not in src


def test_browser_ensure_browser_stops_old_pw():
    """#P0-5：_ensure_browser headless 不匹配时应 stop 旧 playwright"""
    import inspect
    from app.core.douyin import browser
    src = inspect.getsource(browser.BrowserActor._ensure_browser)
    assert "old_pw.stop()" in src
    assert "self._tls.playwright = None" in src


def test_client_uses_curl_cffi():
    """#P0-7：download_video 用 curl_cffi 不再用 urllib"""
    import inspect
    from app.core.douyin import client
    src = inspect.getsource(client.RealDouyinClient.download_video)
    assert "from curl_cffi import requests as creq" in src
    assert 'impersonate="chrome124"' in src
    # download_video 内不应再有 urlopen
    assert "urlopen(" not in src


def test_fetch_aweme_detail_accepts_account_id():
    """#P0-8：_fetch_aweme_detail 应接受 account_id 参数"""
    import inspect
    from app.core.douyin import client
    sig = inspect.signature(client.RealDouyinClient._fetch_aweme_detail)
    assert "account_id" in sig.parameters
    assert sig.parameters["account_id"].default == ""


def test_import_share_links_no_info_undefined():
    """回归测试：import_share_links 内部传 info=None 不应引用未定义变量。

    历史 bug：分享导入循环里 `info=info` 触发 NameError（函数内无 info 形参）。
    修复后传 None，走 _download_and_ingest 内部 time.sleep 降级。
    """
    import inspect
    from app.services import material_service
    src = inspect.getsource(material_service.import_share_links)
    # 修复点：必须传 None 而非 info（info 在函数作用域内未定义）
    assert "info=None" in src, "import_share_links 内 info= 应改为 info=None"
    # 防御性：不应再出现裸 `info=info`（除非 import_share_links 真有 info 形参）
    sig = inspect.signature(material_service.import_share_links)
    assert "info" not in sig.parameters, (
        "import_share_links 签名包含 info 时无需传 None，请同步测试"
    )


def test_parse_aweme_common_download_urls_have_field():
    """验证 _parse_aweme_common 输出 download_urls 为 (field, url) 元组列表。

    设计目标：_download_with_retry 日志要能看出当前节点来自哪个字段
    （play_addr / play_addr_h264 / play_addr_265），便于排查 CDN 节点归属。
    """
    import inspect
    from app.core.douyin import client
    src = inspect.getsource(client._parse_aweme_common)
    # 拼接处必须把字段名和 URL 绑在一起
    assert "dl_list = [(f, u)" in src or '("play_addr", play_list)' in src, (
        "_parse_aweme_common 应输出 (field, url) 元组列表"
    )


def test_download_with_retry_accepts_tuple_list():
    """验证 _download_with_retry 接受 (field, url) 形态的 download_urls。

    应兼容新旧两种形态：
    - 新：[(field, url), ...]
    - 旧：[url, ...]（legacy fallback 标 field='legacy'）
    """
    import inspect
    from app.services import material_service
    src = inspect.getsource(material_service._download_and_ingest)
    # 内联函数 _download_with_retry 应解析 tuple + 兼容裸 URL
    assert "isinstance(item, tuple)" in src, (
        "_download_with_retry 应识别 (field, url) 元组"
    )
    assert '"legacy"' in src, (
        "_download_with_retry 兼容旧形态（裸 URL）时应打 'legacy' 字段名"
    )


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
