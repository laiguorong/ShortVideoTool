# -*- coding: utf-8 -*-
"""诊断 probe：进搜索结果页后保持浏览器打开，监听用户 click 操作 + dump DOM。

用户手动在浏览器里操作筛选面板时：
- 每一次 click 都会被记到日志（element HTML + 坐标 + tag）
- 任何时候 dump 关键字（窗口里说）会保存当前 page.content() 到 tests/_filter_out/

完成后回车关闭浏览器。
"""
import sys
import time
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def main() -> int:
    from app.services.setting_service import init_data_dir, get_data_dir, get_config_dir
    from app.core import crypto
    from app.db.database import init_db, get_db
    init_data_dir()
    crypto.init_crypto(get_config_dir())
    data_dir = get_data_dir()
    init_db(data_dir / "shortvideotool.db")
    d = get_db()

    row = d.query_one(
        "SELECT id, nickname FROM account WHERE deleted=0 AND status IN ('normal','valid') LIMIT 1"
    )
    if not row:
        print("[FAIL] 无可用账号")
        return 1
    account_id = row["id"]

    from app.services.douyin_account import get_profile_dir
    profile_dir = get_profile_dir(account_id)

    from app.core.douyin.search_api import BrowserSearchSession
    # 真机观察
    sess = BrowserSearchSession(profile_dir, headless=False)

    print("[1] 启动浏览器 + 首页...")
    sess._ensure_open()

    keyword = "鱼公元鱼生"
    print(f"[2] 搜索关键词：{keyword}")
    # _click_and_type_keyword 内已 fill+Enter + 触发搜索 XHR（_on_response 自动捕获）
    sess._click_and_type_keyword(sess._page, keyword)
    print(f"    触发搜索后，_captured={len(sess._captured)}")

    try:
        sess._page.wait_for_selector('[data-e2e="search-card"]', timeout=15_000)
        print("    搜索卡片已渲染")
    except Exception:
        print("    [warn] 没找到 search-card，继续")

    # 输出目录
    out_dir = pathlib.Path(__file__).parent / "_filter_out"
    out_dir.mkdir(exist_ok=True)
    # 初始 DOM
    sess._page.evaluate("window.dumpCounter = 0")
    initial = out_dir / "00_initial.html"
    initial.write_text(sess._page.content(), encoding="utf-8")
    sess._page.screenshot(path=str(out_dir / "00_initial.png"))
    print(f"[3] 初始 DOM + 截图已保存 {out_dir}/00_*")

    # 监听 click + mouseover 事件（用户说 hover 触发筛选页签）
    def _make_handler(label):
        def _h(event):
            try:
                txt = sess._page.evaluate(
                    """(el) => {
                        if (!el) return null;
                        return {
                            tag: el.tagName,
                            text: (el.innerText || '').slice(0, 80),
                            cls: (el.className || '').slice(0, 80),
                            attrs: Array.from(el.attributes || []).map(a => a.name + '=' + a.value).join(' ').slice(0, 250),
                            e2e: (() => { let n = el; while (n && n !== document.body) { if (n.dataset && n.dataset.e2e) return n.dataset.e2e; n = n.parentElement; } return null; })(),
                            role: el.getAttribute('role') || '',
                        };
                    }""",
                    event.element,
                )
                ts = datetime.now().strftime("%H:%M:%S")
                sys.stdout.write(f"\n[{label} {ts}] {txt}\n")
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(f"[{label} 异常] {e}\n")
                sys.stdout.flush()
        return _h

    sess._page.on("click", _make_handler("CLICK"))
    sess._page.on("mouseover", _make_handler("HOVER"))

    print("\n[4] 浏览器已就绪，请在浏览器里操作筛选面板。")
    print("    每次 click 都会实时打印 element 详情。")
    print("    每 30s 自动 dump 一次 DOM + 截图到 tests/_filter_out/")
    print("    操作完跟我说「dump now」会立刻 dump，或「quit」让我结束 probe。")

    last_dump = time.time()
    # probe 不依赖 stdin（后台运行无 tty）—— 持续监听 click + 定期 dump
    while True:
        try:
            time.sleep(1)
            # 每 30s 自动 dump
            if time.time() - last_dump >= 30:
                n = sess._page.evaluate("window.dumpCounter || 0") + 1
                sess._page.evaluate(f"window.dumpCounter = {n}")
                ts = datetime.now().strftime("%H%M%S")
                html_p = out_dir / f"auto_{n:02d}_{ts}.html"
                png_p = out_dir / f"auto_{n:02d}_{ts}.png"
                html_p.write_text(sess._page.content(), encoding="utf-8")
                sess._page.screenshot(path=str(png_p))
                print(f"[auto-dump {ts}] {html_p.name} + {png_p.name}")
                last_dump = time.time()
        except KeyboardInterrupt:
            break
        except Exception as e:
            # 浏览器关闭后 page.evaluate 会抛 → 视为退出
            print(f"[probe loop] {e}")
            break

    try:
        sess.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())