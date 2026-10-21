# -*- coding: utf-8 -*-
"""搜索 UI 交互的超时回归测试。

# 线上症状（2026-10-21）：
  用户反馈「输入关键词，点搜索，结果也出来了，然后就不动了，接着应该要切换
  视频页签」。日志显示：
      16:00:46.567  [搜索] 已发起搜索
      16:02:10.775  [Tab] 已切换到视频tab      ← 中间空白 84.2 秒
  第二轮同样卡 61.9 秒。

# 根因：Playwright 动作类 API 的默认 auto-wait 是 30s。
  `locator.evaluate()` / `locator.click()` 在元素不存在时不会立即失败，
  而是死等 30s 才抛 TimeoutError。`_TAB_VIDEO_SELECTORS` 是 2 条链，
  逐条 try/except continue → 最坏 30s × 2 = 60s，加探针 evaluate 一轮 ≈ 84s。
  实测（Playwright 行为验证）：
      count()      → 立即返回 0，不等待
      is_visible() → 立即返回 False，不等待
      evaluate()   → 死等 30s 抛 TimeoutError
      click()      → 死等 30s 抛 TimeoutError

# 修复：所有动作类调用传显式 timeout（_ACTION_TIMEOUT_MS=3s）。
# 本测试锁两件事：
  1. 行为层——selector 失效时必须快速降级返回 False，而不是等 30s×
  2. 源码层——AST 断言动作类调用都带 timeout 参数，防止以后有人"清理代码"
#     时把 timeout 删掉（这类回归极难靠肉眼发现）
"""
import ast
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.douyin.search_api import (
    BrowserSearchSession,
    _ACTION_TIMEOUT_MS,
    _POLL_TIMEOUT_MS,
)

# tab 选择器链是类属性（定义在 BrowserSearchSession 内），不是模块级常量
_TAB_VIDEO_SELECTORS = BrowserSearchSession._TAB_VIDEO_SELECTORS

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def blank_page():
    """一个不含任何 tab/筛选元素的空白页（模拟抖音 UI 改版后的失效场景）。"""
    with playwright_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<div>页面无 tab / 无筛选面板</div>")
        yield page
        browser.close()


def _session() -> BrowserSearchSession:
    """构造一个不启动浏览器的 session（只测纯逻辑方法）。"""
    return BrowserSearchSession.__new__(BrowserSearchSession)


# ============ 行为层：必须快速降级 ============

def test_click_first_visible_returns_fast_when_selectors_fail(blank_page):
    """selector 全部失效时，_click_first_visible 必须秒级返回 False。

    修复前：30s × len(selectors) ≈ 60s。
    """
    s = _session()
    t0 = time.time()
    hit = s._click_first_visible(blank_page, _TAB_VIDEO_SELECTORS, "切换视频tab")
    elapsed = time.time() - t0

    assert hit is False, "selector 失效时应返回 False（按当前 tab 拉取）"
    # 上限给 10s 留余量（正常机器 0.01s，但 CI 慢/负载高时别误伤）
    assert elapsed < 10, f"耗时 {elapsed:.1f}s，超过预期——timeout 参数可能丢了"


def test_switch_to_video_tab_returns_fast_when_tab_missing(blank_page):
    """_switch_to_video_tab 整体（含探针 evaluate）必须秒级返回。"""
    s = _session()
    t0 = time.time()
    ok = s._switch_to_video_tab(blank_page)
    elapsed = time.time() - t0

    assert ok is False
    assert elapsed < 10, f"耗时 {elapsed:.1f}s，超过预期"


def test_action_timeout_constants_are_short():
    """超时常量本身要短（防有人调大常量"以免误判"）。"""
    assert _ACTION_TIMEOUT_MS <= 5_000, "动作超时不应超过 5s"
    assert _POLL_TIMEOUT_MS <= 2_000, "轮询超时不应超过 2s"


# ============ 源码层：AST 锁住 timeout 参数 ============

def _iter_action_calls(path: Path):
    """遍历 AST，产出真正会 auto-wait 的 Playwright 动作调用。

    必须排除低层输入 API——它们不走 locator auto-wait，不传 timeout 是正常的：
      page.mouse.click / page.mouse.move / page.keyboard.type / page.keyboard.press
    这些是"已经知道元素在哪、直接派发事件"，没有"元素是否存在"的等待语义。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # 会 auto-wait 的 locator 级动作
    ACTION_METHODS = {"click", "evaluate", "is_visible", "hover",
                      "bounding_box", "fill", "wait_for_selector"}
    # 不 auto-wait 的低层输入（用 owner 属性名白名单排除）
    NO_AUTOWAIT_OWNERS = {"mouse", "keyboard"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr not in ACTION_METHODS:
            continue
        # f.value 是 owner：page.mouse.click → f.value=Attribute(attr='mouse')
        owner = f.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else None
        if owner_name in NO_AUTOWAIT_OWNERS:
            continue
        yield f.attr, node


def test_all_action_calls_have_timeout():
    """search_api.py 里所有动作类调用必须显式传 timeout。

    唯一豁免：page.wait_for_timeout（不是动作类，是主动 sleep）。
    """
    path = ROOT / "app" / "core" / "douyin" / "search_api.py"
    missing = []
    for name, node in _iter_action_calls(path):
        kws = {k.arg for k in node.keywords}
        if "timeout" not in kws:
            lineno = node.lineno
            src_line = path.read_text(encoding="utf-8").split("\n")[lineno - 1].strip()
            missing.append(f"L{lineno} .{name}()  →  {src_line[:70]}")
    assert not missing, "以下动作调用缺少 timeout（会走 Playwright 默认 30s auto-wait）:\n" + \
                        "\n".join(missing)


def test_switch_to_video_tab_probe_has_timeout():
    """切 tab 探针的 evaluate 必须带 timeout（这是线上 84s 卡点的核心行）。"""
    path = ROOT / "app" / "core" / "douyin" / "search_api.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "_switch_to_video_tab")

    probe_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "evaluate"
    ]
    assert probe_calls, "_switch_to_video_tab 里应还有 evaluate 探针调用"
    for n in probe_calls:
        assert "timeout" in {k.arg for k in n.keywords}, \
            f"L{n.lineno} 切 tab 探针 evaluate 缺 timeout（线上卡 84s 的根因）"