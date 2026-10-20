# -*- coding: utf-8 -*-
"""截 wizard 的所有 network 请求，看 preview-direct 实际发了什么 payload。"""
import os
import sys
import time
import subprocess
import pathlib
import json
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SERVER_PORT = 8766
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "backend" / "assets" / "ms-playwright")


def main() -> int:
    py = str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")
    srv = str(ROOT / "backend" / "tests" / "serve_renderer.py")
    p = subprocess.Popen([py, srv])
    time.sleep(2)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()

            captured = []

            def on_request(req):
                if "preview-direct" in req.url or "publish" in req.url:
                    try:
                        body = req.post_data
                        if body:
                            captured.append({"url": req.url, "body": body})
                    except Exception:
                        pass

            page.on("request", on_request)

            responses = []
            def on_response(resp):
                if "preview-direct" in resp.url:
                    try:
                        responses.append({"url": resp.url, "status": resp.status, "text": resp.text()[:800]})
                    except Exception as e:
                        responses.append({"err": str(e)})
            page.on("response", on_response)

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

            # 跳到发布 + 编辑 draft
            page.get_by_role("button", name="发布").first.click()
            time.sleep(2)
            page.get_by_role("button", name="编辑").first.click()
            time.sleep(2)

            # 走完 1-4 步
            for i in range(4):
                if i == 0:
                    page.get_by_role("button", name="全选").first.click()
                else:
                    try:
                        page.get_by_role("button", name="全选").first.click(timeout=2000)
                    except Exception:
                        pass
                time.sleep(1)
                btn = page.get_by_role("button", name="下一步").first
                for _ in range(20):
                    if btn.is_enabled():
                        break
                    time.sleep(0.5)
                btn.click()
                time.sleep(1.5)

            # step 5 → 截网络请求
            time.sleep(1)
            print("[step 5 ready]")

            # 点预览排期
            preview_btn = page.get_by_role("button", name="预览排期").first
            for _ in range(20):
                if preview_btn.is_enabled():
                    break
                time.sleep(0.5)
            preview_btn.click()
            time.sleep(3)

            print(f"\n[captured] {len(captured)} requests:")
            for c in captured[:5]:
                print(f"\n=== {c['url']} ===")
                # 截取 1500 字符，看 start_time / end_time
                print(c['body'][:2500])

            print(f"\n[responses] {len(responses)}:")
            for r in responses:
                print(f"\n--- status={r.get('status')} ---")
                print(r.get('text', r.get('err')))

            browser.close()
            return 0
    finally:
        p.terminate()
        p.wait(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
