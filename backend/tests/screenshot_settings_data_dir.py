# -*- coding: utf-8 -*-
"""截设置页 → 数据目录 Tab（替换 06-datadir 资源管理器截图）。"""
import os
import sys
import time
import subprocess
import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "docs" / "images" / "06-datadir-v1.0.4-raw.png"
SERVER_PORT = 8766

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "backend" / "assets" / "ms-playwright")


def start_server() -> subprocess.Popen:
    py = str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")
    srv = str(ROOT / "backend" / "tests" / "serve_renderer.py")
    p = subprocess.Popen([py, srv])
    time.sleep(2)
    return p


def main() -> int:
    srv = start_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()

            page.goto(f"http://127.0.0.1:{SERVER_PORT}/index.html",
                       wait_until="domcontentloaded", timeout=30_000)
            for _ in range(30):
                time.sleep(1)
                txt = page.evaluate("document.body.innerText || ''")
                if "工作台" in txt and "账号" in txt:
                    break
            try:
                page.get_by_text("已知晓，同意并继续").first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                pass

            # 跳到设置 Tab
            page.get_by_role("button", name="设置").first.click()
            time.sleep(2)

            # 点 "数据目录" 子 Tab
            page.get_by_role("button", name="数据目录").first.click()
            time.sleep(2)

            page.screenshot(path=str(OUT), full_page=False)
            print(f"[OK] {OUT.name}  size={OUT.stat().st_size}")
            browser.close()
            return 0
    finally:
        srv.terminate()
        srv.wait(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
