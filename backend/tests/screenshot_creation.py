# -*- coding: utf-8 -*-
"""截创作中心：项目列表 + 项目详情 + 添加视频 + 添加音乐。"""
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


def start_server() -> subprocess.Popen:
    py = str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")
    srv = str(ROOT / "backend" / "tests" / "serve_renderer.py")
    p = subprocess.Popen([py, srv])
    time.sleep(2)
    return p


def open_app_and_skip_dialog(page):
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


def main() -> int:
    srv = start_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            open_app_and_skip_dialog(page)

            # 跳到创作 Tab
            page.get_by_role("button", name="创作").first.click()
            time.sleep(2)

            # 截图 1: 创作中心项目列表（已经是默认页）
            out1 = OUT / "09a-creation-list-v1.0.4-raw.png"
            page.screenshot(path=str(out1), full_page=False)
            print(f"[list] {out1.name}  size={out1.stat().st_size}")

            # 点第一个项目的标题行（tr 上的 onClick 触发 setEditingId）
            # 项目行：标题 "鱼公元鱼生探店（演示）"
            try:
                page.locator("td:has-text('鱼公元鱼生探店（演示）')").first.click()
                time.sleep(2.5)
            except Exception as e:
                print(f"[nav] click row failed: {e}")
                # 兜底：找所有 tr 的第一个
                page.locator("tbody tr").first.click()
                time.sleep(2.5)

            # 截图 2: 项目详情页
            out2 = OUT / "09b-creation-detail-v1.0.4-raw.png"
            page.screenshot(path=str(out2), full_page=False)
            print(f"[detail] {out2.name}  size={out2.stat().st_size}")

            # 截图 3: 添加视频弹窗
            try:
                page.get_by_role("button", name="添加视频").first.click()
                time.sleep(2)
                out3 = OUT / "09c-creation-add-video-v1.0.4-raw.png"
                page.screenshot(path=str(out3), full_page=False)
                print(f"[add video] {out3.name}  size={out3.stat().st_size}")
                # 关闭弹窗
                page.keyboard.press("Escape")
                time.sleep(1)
            except Exception as e:
                print(f"[add video] err: {e}")

            # 截图 4: 添加音乐弹窗
            try:
                page.get_by_role("button", name="添加音乐").first.click()
                time.sleep(2)
                out4 = OUT / "09d-creation-add-music-v1.0.4-raw.png"
                page.screenshot(path=str(out4), full_page=False)
                print(f"[add music] {out4.name}  size={out4.stat().st_size}")
                page.keyboard.press("Escape")
                time.sleep(1)
            except Exception as e:
                print(f"[add music] err: {e}")

            browser.close()
            return 0
    finally:
        srv.terminate()
        srv.wait(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
