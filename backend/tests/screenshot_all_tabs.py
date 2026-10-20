# -*- coding: utf-8 -*-
"""遍历所有 Tab 截真实界面。

依赖：
- 后端 uvicorn 在 8765（手动启） + settings.json 已配 data_dir
- bundled Chromium（backend/assets/ms-playwright/）
- HTTP server 在 8766 提供 dist/renderer/（含正确 MIME）

输出：docs/marketing/screenshots/{nn-tabname-v1.0.4-raw}.png
"""
import os
import sys
import time
import pathlib
from playwright.sync_api import sync_playwright, Page

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "marketing" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "backend" / "assets" / "ms-playwright")

HTTP_BASE = "http://127.0.0.1:8766"

# 导航配置：Tab 名 + 截图名 + 等待选择器（保证 Tab 内容渲染完成）
TABS = [
    ("工作台", "01-dashboard",   "text=4 张卡片盯 4 个核心指标 OR text=可用成品 OR [class*='grid']",  "01-dashboard-v1.0.4-raw.png"),
    ("账号",   "02-accounts",    "text=失效 OR text=检测中 OR text=重新登录",                            "02-accounts-v1.0.4-raw.png"),
    ("选品",   "03-selection",   "text=佣金率 OR text=核销率 OR text=已发布",                            "03-selection-v1.0.4-raw.png"),
    ("素材",   "04-materials",   "text=拉取任务 OR text=素材分类 OR text=本地",                          "04-materials-v1.0.4-raw.png"),
    ("创作",   "05-creation",    "text=项目 OR text=分镜 OR text=组合生成 OR text=素材库",                "05-creation-v1.0.4-raw.png"),
    ("发布",   "06-publish",     "text=新建任务 OR text=新建草稿 OR text=任务名",                        "06-publish-v1.0.4-raw.png"),
    ("记录",   "07-records",     "text=导出 OR text=发布记录 OR text=成功",                              "07-records-v1.0.4-raw.png"),
    ("任务",   "08-tasks",       "text=任务队列 OR text=后台任务 OR text=已完成",                        "08-tasks-v1.0.4-raw.png"),
    ("设置",   "09-settings",    "text=数据目录 OR text=备份 OR text=通知",                              "09-settings-v1.0.4-raw.png"),
]


def wait_main_ui(page: Page, timeout_s: int = 30) -> bool:
    """等主界面渲染（侧边栏 + 任一 Tab 标题）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            txt = page.evaluate("document.body.innerText || ''")
        except Exception:
            txt = ""
        if "工作台" in txt and "账号" in txt and "素材" in txt:
            return True
        time.sleep(0.5)
    return False


def close_risk_dialog(page: Page) -> bool:
    """点掉风险告知弹窗（首次启动弹出）。"""
    try:
        # 找"已知晓，同意并继续"按钮
        btn = page.get_by_text("已知晓，同意并继续").first
        if btn.is_visible(timeout=2000):
            btn.click()
            time.sleep(1)
            print("[risk] 风险告知已关闭")
            return True
    except Exception:
        pass
    return False


def goto_tab(page: Page, label: str) -> bool:
    """点击侧边栏某个 Tab。"""
    try:
        # 侧边栏的 Tab 是 button 或 a，文本匹配
        nav_item = page.get_by_role("button", name=label).first
        if nav_item.is_visible(timeout=2000):
            nav_item.click()
            return True
        # fallback：用 text=
        nav_item = page.locator(f"text={label}").first
        nav_item.click()
        return True
    except Exception as e:
        print(f"[nav] 跳转 {label} 失败: {e}")
        return False


def main() -> int:
    print(f"[setup] backend env PLAYWRIGHT_BROWSERS_PATH={os.environ['PLAYWRIGHT_BROWSERS_PATH']}")
    print(f"[setup] http={HTTP_BASE}  out={OUT_DIR}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                   device_scale_factor=1.0)
        page = ctx.new_page()

        errors = []
        page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text))
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        url = f"{HTTP_BASE}/index.html"
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        if not wait_main_ui(page):
            print("[FAIL] 主界面没渲染出来")
            for e in errors[:5]:
                print(f"  - {e[:200]}")
            browser.close()
            return 1

        time.sleep(2)
        close_risk_dialog(page)
        time.sleep(1)

        # 截图每个 Tab
        for label, key, wait_text, fname in TABS:
            print(f"\n[{key}] 跳转 → {label}")
            if not goto_tab(page, label):
                print(f"[{key}] 跳过（导航失败）")
                continue
            # 等 Tab 内容渲染
            time.sleep(2.5)
            out = OUT_DIR / fname
            page.screenshot(path=str(out), full_page=False)
            print(f"[{key}] {fname}  size={out.stat().st_size}")

        # 截图后报告错误
        if errors:
            print(f"\n[errors] {len(errors)}:")
            seen = set()
            for e in errors:
                sig = e[:120]
                if sig in seen:
                    continue
                seen.add(sig)
                print(f"  - {e[:200]}")

        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
