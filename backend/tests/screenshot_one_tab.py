# -*- coding: utf-8 -*-
"""截单个 Tab（用于补截）。"""
import os
import sys
import time
import subprocess
import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "docs" / "marketing" / "screenshots"
SERVER_PORT = 8766

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "backend" / "assets" / "ms-playwright")


def main(tab_label: str, out_name: str) -> int:
    py = str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")
    srv = str(ROOT / "backend" / "tests" / "serve_renderer.py")
    p = subprocess.Popen([py, srv])
    time.sleep(2)

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

            page.get_by_role("button", name=tab_label).first.click()
            time.sleep(2.5)

            out_path = OUT / out_name
            page.screenshot(path=str(out_path), full_page=False)
            print(f"[OK] {out_path.name}  size={out_path.stat().st_size}")
            browser.close()
            return 0
    finally:
        p.terminate()
        p.wait(timeout=3)


if __name__ == "__main__":
    tab = sys.argv[1]
    out_name = sys.argv[2]
    sys.exit(main(tab, out_name))
