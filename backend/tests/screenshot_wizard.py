# -*- coding: utf-8 -*-
"""截 6 步向导所有步骤（6 张图）+ 创作中心详情页（待补）。"""
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


def click_next(page) -> bool:
    """点'下一步'按钮，等 enable。"""
    btn = page.get_by_role("button", name="下一步").first
    for _ in range(20):
        if btn.is_enabled():
            break
        time.sleep(0.5)
    if not btn.is_enabled():
        return False
    btn.click()
    time.sleep(1.5)
    return True


def click_preview(page) -> bool:
    """step 5 专用：点'预览排期'按钮（计算 preview API）。"""
    btn = page.get_by_role("button", name="预览排期").first
    for _ in range(30):
        if btn.is_enabled():
            break
        time.sleep(0.5)
    if not btn.is_enabled():
        return False
    btn.click()
    # 等 preview API 返回 + UI 切到 step 6
    time.sleep(4)
    return True


def main() -> int:
    srv = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
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

            # 跳到发布 Tab
            page.get_by_role("button", name="发布").first.click()
            time.sleep(2)

            # 点 draft 任务的"编辑"按钮
            page.get_by_role("button", name="编辑").first.click()
            time.sleep(2)

            # ===== step 1: 选择账号 =====
            page.get_by_role("button", name="全选").first.click()
            time.sleep(1)
            out1 = OUT / "03-wizard-step1-v1.0.4-raw.png"
            page.screenshot(path=str(out1), full_page=False)
            print(f"[step1] {out1.name}  size={out1.stat().st_size}")
            if not click_next(page):
                print("[FAIL] step 1 → step 2 失败")
                return 1

            # ===== step 2: 选择项目 =====
            time.sleep(0.5)
            try:
                page.get_by_role("button", name="全选").first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                pass
            out2 = OUT / "03-wizard-step2-v1.0.4-raw.png"
            page.screenshot(path=str(out2), full_page=False)
            print(f"[step2] {out2.name}  size={out2.stat().st_size}")
            if not click_next(page):
                print("[FAIL] step 2 → step 3 失败")
                return 1

            # ===== step 3: 挂载门店 =====
            time.sleep(0.5)
            try:
                page.get_by_role("button", name="全选").first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                pass
            out3 = OUT / "03-wizard-step3-v1.0.4-raw.png"
            page.screenshot(path=str(out3), full_page=False)
            print(f"[step3] {out3.name}  size={out3.stat().st_size}")
            if not click_next(page):
                print("[FAIL] step 3 → step 4 失败")
                return 1

            # ===== step 4: 视频简介 =====
            time.sleep(1)
            # 全选视频简介（让 step 6 排期明细有简介内容）
            try:
                page.get_by_role("button", name="全选").first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                # 兜底：勾选所有 checkbox
                page.locator('input[type=checkbox]').nth(1).click()  # 跳过项目模式 radio
                time.sleep(0.5)
            out4 = OUT / "03-wizard-step4-v1.0.4-raw.png"
            page.screenshot(path=str(out4), full_page=False)
            print(f"[step4] {out4.name}  size={out4.stat().st_size}")
            if not click_next(page):
                print("[FAIL] step 4 → step 5 失败")
                return 1

            # ===== step 5: 排期规则 =====
            time.sleep(1)

            # 关键：起始时间必须晚于当前，否则前端校验 "起始不能选过去" 阻断 preview
            # React controlled input 必须用 native setter 才能触发 onChange
            page.evaluate(
                """() => {
                    const inputs = document.querySelectorAll('input[type="datetime-local"]');
                    const tomorrow = new Date(Date.now() + 86400000);
                    const yyyy = tomorrow.getFullYear();
                    const mm = String(tomorrow.getMonth() + 1).padStart(2, '0');
                    const dd = String(tomorrow.getDate()).padStart(2, '0');
                    const values = [`${yyyy}-${mm}-${dd}T10:00`, `${yyyy}-${mm}-${dd}T22:00`];
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    inputs.forEach((el, i) => {
                        nativeSetter.call(el, values[i] || values[0]);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    });
                }"""
            )
            time.sleep(1)
            # 验证 React state 是否真的更新了
            actual = page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type=datetime-local]')).map(i => i.value)"""
            )
            print(f"[time after patch] {actual}")

            out5 = OUT / "03-wizard-step5-v1.0.4-raw.png"
            page.screenshot(path=str(out5), full_page=False)
            print(f"[step5] {out5.name}  size={out5.stat().st_size}")

            # ===== step 6: 预览确认 =====
            if not click_preview(page):
                print("[WARN] 点预览排期失败，跳过 step 6")
                browser.close()
                return 0

            # 验证是否真的到 step 6
            txt = page.evaluate("document.body.innerText || ''")
            if "预览确认" not in txt or "排期明细" not in txt:
                print(f"[WARN] preview 似乎没切到 step 6: {txt[:300]}")
                # 也截一张作 fallback
            out6 = OUT / "03-wizard-step6-v1.0.4-raw.png"
            page.screenshot(path=str(out6), full_page=False)
            print(f"[step6] {out6.name}  size={out6.stat().st_size}")

            browser.close()
            return 0
    finally:
        srv.terminate()
        srv.wait(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
