# -*- coding: utf-8 -*-
"""v4 探测：用户手动操作浏览器 + 我们监听搜索 XHR。

设计：
- 启动 headless=False 的 BrowserSearchSession（不进 with，不会自动关闭）
- fill+Enter 触发搜索进入结果页
- 探测可能的"加载更多"机制（sentinel / 按钮 / store / 全局函数）
- 等用户手动操作（滚动 / 鼠标 / 键盘 / 点击），监听器累积捕获
- 每秒打印累积条数，用户随时可以 Ctrl+C 退出

不主动 close：用户能继续在浏览器里操作，看累积效果。
"""
import sys
import pathlib
import time as _time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

KEYWORD = "鱼公元鱼生"


def main() -> int:
    from app.services.setting_service import init_data_dir
    init_data_dir()

    profile_dir = pathlib.Path(
        "D:/ShortVideoToolData/accounts/31aaa6476a044966967bd3585e6e0f13/profile"
    )
    print(f"[profile] {profile_dir}")

    from app.core.douyin.search_api import BrowserSearchSession, SearchBlockedError

    # 不进 with，避免自动关闭浏览器
    sess = BrowserSearchSession(profile_dir, headless=False)
    sess._ensure_open()
    page = sess._page

    # 注册详细 XHR 监听（只抓 general/search/single）
    captured: list[tuple[str, dict]] = []

    def on_resp(r):
        if "general/search/single" not in r.url:
            return
        try:
            body = r.json()
        except Exception:
            return
        url = r.url
        captured.append((url, body))
        ids = [
            ((it.get("aweme_info") or {}).get("aweme_id"))
            for it in (body.get("data") or [])[:5]
        ]
        print(
            f"  [XHR {len(captured):02d}] status=200 cursor={body.get('cursor')} "
            f"raw={len(body.get('data') or [])} ids={ids}"
        )

    page.on("response", on_resp)

    # 1. fill+Enter 触发首屏
    print("\n=== 1. fill+Enter 触发搜索 ===")
    inp = page.locator('input[data-e2e="searchbar-input"]').first
    inp.fill(KEYWORD)
    inp.press("Enter")
    page.wait_for_timeout(8000)
    print(f"[状态] url={page.url}")
    print(f"[状态] title={page.title()!r}")
    print(f"[状态] scrollHeight={page.evaluate('document.body.scrollHeight')} "
          f"scrollY={page.evaluate('window.scrollY')}")
    print(f"[状态] captured={len(captured)}")

    # 2. 探测可能的触发机制
    print("\n=== 2. 探测触发机制 ===")

    # 2a. 视口底部附近的 sentinel 候选
    print("\n--- 2a. 视口底部附近的元素（IntersectionObserver sentinel 候选） ---")
    sentinel = page.evaluate("""() => {
        const all = document.querySelectorAll('*');
        const out = [];
        for (const el of all) {
            const r = el.getBoundingClientRect();
            if (r.top > window.innerHeight * 0.7 && r.top < window.innerHeight * 2) {
                out.push({
                    tag: el.tagName,
                    cls: el.className.toString().slice(0, 40),
                    top: Math.round(r.top),
                    height: Math.round(r.height),
                });
            }
        }
        return out.slice(0, 10);
    }""")
    print(sentinel)

    # 2b. 找'加载更多'按钮
    print("\n--- 2b. '加载更多'类按钮 ---")
    btns = page.evaluate("""() => {
        const all = document.querySelectorAll('button, [role=button], a, span, div, li');
        const out = [];
        for (const el of all) {
            const t = (el.textContent || '').trim();
            if (t.length > 0 && t.length < 30 && /加载|更多|下一页|查看/.test(t)) {
                out.push({tag: el.tagName, text: t, cls: el.className.toString().slice(0, 30)});
            }
        }
        return out.slice(0, 10);
    }""")
    print(btns)

    # 2c. 找 React/Vue 全局 store / state
    print("\n--- 2c. 全局 store/state 对象（可能含加载函数） ---")
    stores = page.evaluate("""() => {
        const found = {};
        for (const k of Object.keys(window)) {
            if (/search|store|state|reducer|dispatch|context/i.test(k)) {
                try {
                    const v = window[k];
                    if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
                        const ks = Object.keys(v).slice(0, 8);
                        found[k] = ks;
                    }
                } catch(e) {}
            }
        }
        return found;
    }""")
    print(stores if stores else "（无）")

    # 2d. 监听用户操作后的状态变化
    print("\n=== 3. 等待用户手动操作（60s 内） ===")
    print("请在浏览器里尝试：")
    print("  - 滚动鼠标滚轮到底部")
    print("  - 拖动滚动条")
    print("  - 按 End / PageDown / Space 键")
    print("  - 点击底部任意元素")
    print("  - 移动鼠标到不同位置")
    print("观察控制台输出，看哪种操作触发 XHR\n")

    initial_captured = len(captured)
    last_captured_count = initial_captured

    for i in range(60):
        page.wait_for_timeout(1000)
        cur = len(captured)
        if cur > last_captured_count:
            # 打印状态
            print(f"[{i+1:02d}s] captured 新增 {cur - last_captured_count} 条，"
                  f"scrollY={page.evaluate('window.scrollY')}, "
                  f"scrollHeight={page.evaluate('document.body.scrollHeight')}")
            last_captured_count = cur
        elif i % 10 == 9:
            print(f"[{i+1:02d}s] 还活着，captured={cur}")

    # 4. 尝试主动触发：dispatchEvent on body
    print("\n=== 4. 主动尝试：派发 wheel/scroll 事件 ===")
    before = len(captured)
    page.evaluate("""() => {
        window.dispatchEvent(new WheelEvent('wheel', {deltaY: 1000, bubbles: true}));
        window.dispatchEvent(new Event('scroll'));
        document.dispatchEvent(new Event('scroll'));
        // 派发到 body
        document.body.dispatchEvent(new Event('scroll'));
        // 强制设置 scrollY
        Object.defineProperty(window, 'scrollY', {value: 9999, writable: true});
        window.dispatchEvent(new Event('scroll'));
    }""")
    page.wait_for_timeout(3000)
    print(f"派发事件后 captured 新增: {len(captured) - before}")

    # 5. 鼠标移动到不同位置（hover 监听器可能触发）
    print("\n=== 5. 鼠标移动到搜索结果页不同位置 ===")
    before = len(captured)
    for x in (200, 400, 600, 800, 1000, 1200):
        page.mouse.move(x, 400)
        page.wait_for_timeout(500)
    print(f"鼠标移动后 captured 新增: {len(captured) - before}")

    # 6. 让用户继续操作（不限时）
    print("\n=== 6. 继续等用户操作（30s） ===")
    print("再试一些操作...")

    initial = len(captured)
    for i in range(30):
        page.wait_for_timeout(1000)
        if len(captured) > initial:
            print(f"[{i+1}s] 用户操作触发 XHR，新增 {len(captured) - initial} 条")
            initial = len(captured)

    print(f"\n=== 探测完成，捕获 {len(captured)} 条 XHR ===")
    print("浏览器仍打开，用户可继续操作")
    print("按 Ctrl+C 退出（不自动关闭浏览器）")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\n[用户 Ctrl+C 退出]")
        rc = 0
    # 不 sys.exit：避免 Python 退出时强杀浏览器子进程
    # 用户操作完后手动 Ctrl+C 关终端即可（浏览器由 OS 回收）
    print("\n[probe 已结束，浏览器保持打开]")
    print("如需关闭浏览器，请到任务管理器杀掉 playwright/chromium 进程")
    # 进入死循环，hold 进程不退出
    try:
        while True:
            _time.sleep(60)
    except KeyboardInterrupt:
        pass