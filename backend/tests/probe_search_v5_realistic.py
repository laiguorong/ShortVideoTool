# -*- coding: utf-8 -*-
"""v5 探测：模拟真实用户操作序列触发抖音搜索结果页"加载更多"。

之前 fill/Enter/mouse.move/wheel 派发都失败，是因为它们不触发 React 受控组件的
完整 input/change/keydown/keyup 事件链。真实用户操作：
1. 鼠标点击搜索框（focus + mousedown + mouseup + click）
2. 键盘输入关键词（每个字符派发 keydown/keypress/keyup/input）
3. 键盘按 Enter（提交搜索）
4. 鼠标在结果页向下滚动（wheel 事件 deltaY）

新方案：用 page.mouse.click + page.keyboard.type + page.keyboard.press + page.mouse.wheel
真实模拟，每步记录 XHR 捕获。

持续滚动直到 has_more=0 或 60s 无新 XHR。
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

    from app.core.douyin.search_api import BrowserSearchSession

    sess = BrowserSearchSession(profile_dir, headless=False)
    sess._ensure_open()
    page = sess._page

    captured: list[tuple[str, dict]] = []

    def on_resp(r):
        if "general/search/single" not in r.url:
            return
        try:
            body = r.json()
        except Exception:
            return
        captured.append((r.url, body))
        ids = [
            ((it.get("aweme_info") or {}).get("aweme_id"))
            for it in (body.get("data") or [])[:5]
        ]
        print(
            f"  [XHR {len(captured):02d}] cursor={body.get('cursor')} "
            f"raw={len(body.get('data') or [])} ids={ids}"
        )

    page.on("response", on_resp)

    # 1. 真实点击搜索框（用搜索框位置）
    print("\n=== 1. 真实点击搜索框 ===")
    box = page.locator('input[data-e2e="searchbar-input"]').first.bounding_box()
    if not box:
        print("[FAIL] 找不到搜索框")
        return 1
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    print(f"[搜索框] 位置 ({cx}, {cy}) 大小 {box['width']}x{box['height']}")
    page.mouse.move(cx, cy)
    page.mouse.click(cx, cy)
    page.wait_for_timeout(500)

    # 2. 真实键盘输入关键词（每个字符触发 input 事件）
    print(f"\n=== 2. 真实键盘输入 '{KEYWORD}' ===")
    page.keyboard.type(KEYWORD, delay=80)  # 每个字符延迟 80ms，模拟真人打字速度
    page.wait_for_timeout(500)
    val = page.evaluate(
        'document.querySelector(\'input[data-e2e="searchbar-input"]\').value'
    )
    print(f"[input.value] {val!r}")

    # 3. 真实键盘按 Enter
    print("\n=== 3. 真实键盘按 Enter ===")
    page.keyboard.press("Enter")
    page.wait_for_timeout(8000)
    print(f"[状态] url={page.url}")
    print(f"[状态] title={page.title()!r}")
    print(f"[状态] captured={len(captured)}")

    # 4. 持续向下滚动直到 has_more=0 或 60s 无新 XHR
    print("\n=== 4. 持续向下滚动（真实 wheel）直到无翻页 ===")
    page.mouse.move(700, 500)
    last_progress_ts = _time.time()
    while True:
        before = len(captured)
        # 真实 wheel 5 次模拟用户连续滚动
        for _ in range(5):
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(150)
        page.wait_for_timeout(1500)
        delta = len(captured) - before
        if delta > 0:
            last_body = captured[-1][1]
            cursor = last_body.get("cursor")
            has_more = last_body.get("has_more")
            raw = len(last_body.get("data") or [])
            print(f"  [滚动] 触发 +{delta} | cursor={cursor} has_more={has_more} "
                  f"raw={raw} | captured={len(captured)}")
            last_progress_ts = _time.time()
            if has_more == 0:
                print(f"  [结束] has_more=0，无更多数据")
                break
        else:
            elapsed = int(_time.time() - last_progress_ts)
            sy = page.evaluate("window.scrollY")
            sh = page.evaluate("document.body.scrollHeight")
            print(f"  [静默 | {elapsed}s] 无新 XHR | scrollY={sy} scrollHeight={sh}")
            if elapsed >= 60:
                print(f"  [结束] 60s 无进展，停止")
                break

    # 5. 找"暂时没有更多了"元素确认
    print("\n=== 5. 找底部'暂时没有更多了'元素 ===")
    end_text = page.evaluate("""() => {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            const t = (el.textContent || '').trim();
            if (/暂时没有|没有更多|已加载完|到底了/.test(t) && t.length < 50) {
                return {tag: el.tagName, text: t, cls: el.className.toString().slice(0, 50)};
            }
        }
        return null;
    }""")
    print(f"底部提示元素: {end_text}")

    # 解析捕获数据
    all_ids = []
    for url, body in captured:
        for it in (body.get("data") or []):
            aid = ((it.get("aweme_info") or {}).get("aweme_id")) or it.get("aweme_id")
            if aid:
                all_ids.append(aid)
    print(f"\n[数据汇总] 共 {len(all_ids)} 条 aweme_id，去重 {len(set(all_ids))} 条")
    print(f"[XHR 总数] {len(captured)}")
    print(f"[首页 URL 示例] {captured[0][0][:200] if captured else '无'}")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\n[用户 Ctrl+C 退出]")
        rc = 0
    except Exception as e:
        print(f"\n[异常] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        rc = 99
    print(f"\n[probe 已结束，rc={rc}，浏览器保持打开]")
    try:
        while True:
            _time.sleep(60)
    except KeyboardInterrupt:
        pass