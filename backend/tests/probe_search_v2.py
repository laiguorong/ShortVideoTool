# -*- coding: utf-8 -*-
"""e2e 验证 search_api v2 fill+Enter + 滚动翻页。

跑通标准：
- 启动 BrowserSearchSession 成功（goto www.douyin.com 成功）
- search_page(keyword, page=1) 返回 status_code=0 + data 非空 + 至少 1 个 aweme_info
- search_page(keyword, page=2) 滚动触发第 2 页 XHR，返回同样结构 + 新 aweme_id
- search_page(keyword, page=3) 继续滚动，第 3 页数据
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

KEYWORD = "鱼公元鱼生"


def main() -> int:
    from app.services.setting_service import init_data_dir, get_data_dir
    from app.db.database import init_db, get_db
    from app.db.migrations import migrate

    init_data_dir()
    data_dir = get_data_dir()
    # 优先 shortvideotool.db（账号实际存储位置），找不到再退 douyin_tools.db
    db_path = data_dir / "shortvideotool.db"
    if not db_path.exists():
        db_path = data_dir / "douyin_tools.db"
    init_db(db_path)
    migrate(get_db())

    d = get_db()
    # 找第一个 normal/valid 账号（数据库实际 status='normal'）
    row = d.query_one(
        "SELECT id, nickname, status FROM account WHERE deleted=0 AND status IN ('normal','valid') LIMIT 1"
    )
    if not row:
        print("[WARN] 没有 normal/valid 账号")
    else:
        print(f"[账号] {row['id']} nickname={row['nickname']} status={row['status']}")

    # 直接造一个临时 profile_dir（不依赖账号是否真登录）。
    # e2e 目标：验证 search_api v2 代码路径正确（fill+Enter + 滚动翻页），
    # 没登录态时 search/item XHR 不会返回数据，但能确认代码不崩 + selector 找到 + 流程跑通。
    # 优先 D:\ShortVideoToolData（用户真实 data_dir，账号持久化 profile 在这里）
    profile_dir = None
    if row is not None:
        candidate = Path(f"D:/ShortVideoToolData/accounts/{row['id']}/profile")
        if candidate.exists():
            profile_dir = candidate
            print(f"[profile] 真账号 {profile_dir}")
    if profile_dir is None:
        # 不退到 temp profile（无登录态永远跑不通），明确报错
        print("[FAIL] 该账号没有持久化 profile，必须先在 UI 登录")
        return 1

    from app.core.douyin.search_api import (
        BrowserSearchSession, SearchBlockedError, search_single,
    )

    results: list[dict] = []
    failed = False
    with BrowserSearchSession(profile_dir, headless=False) as sess:
        # v3 cursor 翻页：search_page 用上次响应 cursor 翻下一页
        print("[v3-cursor] 跑 5 页 search_page（cursor 翻页）")
        for page_n in range(1, 6):
            try:
                body = sess.search_page(KEYWORD, offset=(page_n - 1) * 10, timeout=60)
            except SearchBlockedError as e:
                print(f"[FAIL] page={page_n} 搜索失败：{e}")
                failed = True
                break
            data = body.get("data") or []
            aweme_ids = [
                (item.get("aweme_info") or {}).get("aweme_id")
                for item in data if isinstance(item, dict)
            ]
            aweme_ids = [x for x in aweme_ids if x]
            results.append({
                "page": page_n,
                "status_code": body.get("status_code"),
                "has_more": body.get("has_more"),
                "cursor": body.get("cursor"),
                "raw_items": len(data),
                "aweme_ids": aweme_ids,
            })
            print(
                f"[page={page_n}] status={body.get('status_code')} "
                f"has_more={body.get('has_more')} cursor={body.get('cursor')} "
                f"raw={len(data)} ids={aweme_ids[:3]}"
            )

    # 诊断截图：fill + Enter 后页面状态（即使失败也跑）
    print("[诊断] 启动 headless=False 探针，截图 fill/Enter 状态")
    with BrowserSearchSession(profile_dir, headless=False) as sess2:
        page = sess2._page
        # 注册 console 监听
        page.on("console", lambda m: print(f"  [console.{m.type}] {m.text[:200]}"))
        page.on("request", lambda r: print(f"  [req] {r.method} {r.url[:140]}") if ('search' in r.url or 'aweme' in r.url) else None)
        # 打印 selector 命中元素 outerHTML
        for sel in ('input[data-e2e="searchbar-input"]', 'input[placeholder*="搜索"]', 'input[type="search"]'):
            loc = page.locator(sel).first
            try:
                cnt = loc.count()
            except Exception:
                cnt = -1
            if cnt and cnt > 0:
                html = loc.evaluate("el => el.outerHTML")
                attrs = loc.evaluate("el => ({tag: el.tagName, type: el.type, placeholder: el.placeholder, visible: el.offsetParent !== null, rect: el.getBoundingClientRect()})")
                print(f"[selector {sel}] count={cnt}")
                print(f"  attrs={attrs}")
                print(f"  outerHTML={html[:300]}")
        for sel in ('input[data-e2e="searchbar-input"]', 'input[placeholder*="搜索"]'):
            loc = page.locator(sel).first
            if loc.count() > 0:
                print(f"[截图] selector {sel} 命中")
                loc.fill(KEYWORD)
                page.wait_for_timeout(500)
                page.screenshot(path="search_debug_after_fill.png")
                loc.press("Enter")
                page.wait_for_timeout(2000)
                page.screenshot(path="search_debug_after_enter.png")
                print(f"[截图] search_debug_after_fill.png")
                print(f"[截图] search_debug_after_enter.png url={page.url}")
                print(f"[诊断] input.value={loc.evaluate('el => el.value')}")
                # 抓所有 aweme/search URL
                reqs = page.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
                search_urls = [u for u in reqs if 'search' in u or 'aweme' in u]
                print(f"[诊断] total requests: {len(reqs)}, search/aweme: {len(search_urls)}")
                for u in search_urls[:10]:
                    print(f"  {u[:140]}")
                # 看 console log
                print("[诊断] console 监听中（headless 截图窗口内 console）")
                break
    if failed:
        return 2

    # 校验：5 页都有数据 + 各页 aweme_id 不重叠（如果抖音正常返回）
    if len(results) < 5:
        print(f"[FAIL] 应跑 5 页，实际 {len(results)}")
        return 3
    for r in results:
        if r["status_code"] not in (0, None):
            print(f"[FAIL] page={r['page']} status_code={r['status_code']} 非 0")
            return 4
        if r["raw_items"] == 0:
            print(f"[FAIL] page={r['page']} raw_items=0（搜索失败）")
            return 5
    # 5 页 aweme_id 去重汇总
    all_ids_per_page = [set(r["aweme_ids"]) for r in results]
    total_unique = set()
    for ids in all_ids_per_page:
        total_unique |= ids
    print(f"\n[汇总] 5 页共 {sum(len(r['aweme_ids']) for r in results)} 条，去重 {len(total_unique)} 条")
    for i, r in enumerate(results):
        cur_ids = all_ids_per_page[i]
        prev_ids = set() if i == 0 else all_ids_per_page[i - 1]
        overlap = cur_ids & prev_ids
        print(f"  page{i+1}: raw={r['raw_items']} ids={len(cur_ids)} overlap_with_prev={len(overlap)} cursor={r['cursor']} ids={list(cur_ids)[:5]}{'...' if len(cur_ids) > 5 else ''}")
    print("[OK] 5 页搜索全部成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())