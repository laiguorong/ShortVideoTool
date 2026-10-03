# -*- coding: utf-8 -*-
"""发布动作同步版：从 tests/account_isolation/douyin_step2_publish.py
核心逻辑移植而来（去掉 async → 同步 Playwright 语义；api 接口签名稳定）。

设计：
- 纯同步函数 + Playwright sync API（与 backend/browser.py 一致）；
- 所有步骤单独可调用，供 publish_video 编排；
- 捕获 create_v2 POST 响应提取 item_id（线上视频 ID）。

调用入口：sync_publish_video(video_path, storage, title, declaration,
                              location, schedule, allow_save) -> dict
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from loguru import logger

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

# 匹配 create_v2 响应里的线上视频 ID（10+ 位数字，命名兼容 aweme_id / item_id / video_id）
_ID_RE = re.compile(r'"(aweme_id|item_id|video_id|create_id|post_id|work_id)"\s*:\s*"?(\d{10,})"?')


# ---------- 工具函数（同步版） ----------

def safe_screenshot(page, name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:
        pass
    return p


def wait_upload_done(page, timeout: int = 300) -> bool:
    """等上传完成：DOM 含"重新上传 / 替换视频 / 再次上传 / 上传完成"文案。"""
    logger.info("[upload] 等待上传完成...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            content = page.content()
            if any(k in content for k in ["重新上传", "替换视频", "再次上传", "上传完成"]):
                logger.info("[upload] 上传完成")
                return True
            if 'aria-valuenow="100"' in content:
                logger.info("[upload] 进度条 100%")
                return True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    logger.warning("[upload] 上传超时")
    return False


def dismiss_unfinished_banner(page, account_dir: Path) -> dict:
    """关未发布视频残留 banner（"放弃"残留避免阻挡本次发布）。"""
    log = {"detected": False, "action": None}
    try:
        body = page.evaluate("() => document.body.innerText")
        if "未发布的视频" in body and "继续编辑" in body:
            log["detected"] = True
            logger.info("[banner] 检测到未发布视频残留 banner")
            safe_screenshot(page, "step2_banner_detected", account_dir)
            for txt in ["放弃", "丢弃", "不再提示", "直接发布新作品"]:
                try:
                    btn = page.get_by_role("button", name=txt).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        log["action"] = txt
                        logger.info(f"[banner] 点 {txt}")
                        page.wait_for_timeout(2000)
                        return log
                except Exception:
                    continue
            # 兜底：文本节点定位
            clicked = page.evaluate(
                """() => {
                    for (const el of document.querySelectorAll('span, button, a')) {
                        const t = (el.innerText || '').trim();
                        if (t === '放弃' || t === '丢弃') { el.click(); return t; }
                    }
                    return null;
                }"""
            )
            if clicked:
                log["action"] = clicked
                logger.info(f"[banner] 点 {clicked}（文本定位）")
                page.wait_for_timeout(2000)
    except Exception as exc:
        log["error"] = str(exc)
    return log


def fill_title(page, title: str) -> bool:
    """填作品简介（富文本编辑器 class 含 zone-container editor-kit-cont）。"""
    # 抖音简介末尾必须有空格分隔话题区，否则 #话题 被吞
    title_with_space = title if title.endswith(" ") else title + " "
    editor = page.locator('div.zone-container.editor-kit-cont, div[contenteditable="true"]').first
    try:
        editor.wait_for(state="visible", timeout=8000)
        editor.click()
        page.wait_for_timeout(300)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(200)
        page.keyboard.type(title_with_space, delay=50)
        logger.info(f"[fill] 作品简介(富文本): {title_with_space!r}")
        return True
    except Exception as exc:
        logger.warning(f"[fill] 富文本编辑器异常: {exc}")
    # 兜底：textarea placeholder
    for sel in [
        'textarea[placeholder*="作品简介"]',
        'textarea[placeholder*="好的内容"]',
        'div[contenteditable="true"]',
    ]:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=3000)
            loc.fill(title_with_space)
            logger.info(f"[fill] 作品简介(fallback {sel}): {title_with_space!r}")
            return True
        except Exception:
            continue
    logger.warning("[fill] 找不到作品简介输入框")
    return False


def set_self_declaration(page, declaration: str) -> dict:
    """设置自主声明（selectText 下拉 + 半 design 弹层）。"""
    log: dict = {"steps": [], "selected": None, "skipped": False, "verify": None}
    try:
        page.wait_for_timeout(1500)
        sel = page.locator('span.selectText-XSrMFZ').filter(has_text="自主声明").first
        if sel.count() == 0:
            sel = page.locator('span.selectText-XSrMFZ').first
        try:
            sel.wait_for(state="visible", timeout=8000)
        except Exception:
            sel = page.get_by_text("请选择自主声明", exact=False).first
            sel.wait_for(state="visible", timeout=8000)
        sel.click(timeout=5000)
        log["steps"].append("open_select")
        page.wait_for_timeout(2000)

        clicked = False
        try:
            res = page.evaluate(
                """(targetText) => {
                    for (const lb of document.querySelectorAll('label')) {
                        const txt = (lb.innerText || '').trim();
                        if (txt === targetText || txt.includes(targetText)) {
                            const inp = lb.querySelector('input[type="radio"]');
                            if (inp) { inp.click(); return true; }
                        }
                    }
                    for (const inp of document.querySelectorAll('input[type="radio"]')) {
                        const lb = inp.closest('label');
                        const txt = lb ? (lb.innerText || '').trim() : '';
                        if (txt === targetText || txt.includes(targetText)) { inp.click(); return true; }
                    }
                    return false;
                }""",
                declaration,
            )
            if res:
                clicked = True
        except Exception as exc:
            log["js_click_err"] = str(exc)
        if not clicked:
            opt = page.get_by_text(declaration, exact=False).first
            if opt.count() > 0:
                opt.click()
                clicked = True
        if clicked:
            log["steps"].append("select_option")
            log["selected"] = declaration
            logger.info(f"[declaration] 已选: {declaration}")
        else:
            log["warning"] = f"未找到选项: {declaration}"
        page.wait_for_timeout(800)

        # 点确定（modal scope 内）
        try:
            res = page.evaluate(
                """() => {
                    for (const sel of ['.semi-modal-content button.semi-button-primary',
                                       '.semi-modal-content button',
                                       '[role="dialog"] button']) {
                        for (const b of document.querySelectorAll(sel)) {
                            const t = (b.innerText || '').trim();
                            if (t === '确定') { b.click(); return true; }
                        }
                    }
                    return false;
                }"""
            )
            if res:
                log["steps"].append("confirm_modal")
                logger.info("[declaration] 点确定（modal scope）")
            else:
                ok = page.get_by_role("button", name="确定").first
                if ok.count() > 0 and ok.is_visible():
                    ok.click()
                    log["steps"].append("confirm_locator")
        except Exception as exc:
            log["confirm_err"] = str(exc)
        page.wait_for_timeout(1500)

        # 验证
        try:
            cur_text = page.evaluate(
                """(target) => {
                    let el = document.querySelector('span.selectText-XSrMFZ');
                    if (el && el.innerText.trim()) return el.innerText.trim();
                    return null;
                }""",
                declaration,
            )
            log["selectText_text"] = cur_text
            if cur_text and declaration in cur_text:
                log["verify"] = "ok"
            elif cur_text and cur_text != "请选择自主声明":
                log["verify"] = "different"
            else:
                log["verify"] = "fail"
        except Exception as exc:
            log["verify_err"] = str(exc)
    except Exception as exc:
        log["error"] = str(exc)
        log["warning"] = f"自主声明异常: {exc}"
    return log


def set_location(page, location: str) -> dict:
    """设置地理位置（POI）：点下拉 → 搜索 → 点 POI → ESC 关弹层。

    参数:
        location: 门店**名称**（抖音搜索按名称匹配，不再走 poi_id）。
            - 空字符串：跳过（无 POI 发布）
            - 非空但搜索结果找不到：直接抛 LocationNotFoundError（让发布失败）
    """
    log: dict = {"steps": [], "selected": None}
    # 空 location = 跳过 POI 设置
    if not location or not location.strip():
        log["skipped"] = "empty_location"
        return log
    try:
        sel = page.locator('span.selectText-XSrMFZ:has-text("输入地理位置")').first
        if sel.count() == 0:
            sel = page.get_by_text("输入地理位置").first
        sel.click(timeout=5000)
        log["steps"].append("open_dropdown")
        page.wait_for_timeout(2500)

        search = page.locator('input.semi-input.semi-input-default').last
        if search.count() > 0:
            search.click()
            search.fill(location)
            log["steps"].append("search")
            try:
                page.wait_for_selector(
                    "div.name-QUhTI4, div[class*='no-result'], div[class*='loading']",
                    timeout=15000,
                )
            except Exception:
                pass
            page.wait_for_timeout(4000)

        # 点"国内" tab
        try:
            cn_tab = page.get_by_text("国内", exact=True).first
            if cn_tab.count() > 0:
                cls = cn_tab.get_attribute("class") or ""
                if "active" not in cls.lower():
                    cn_tab.click(timeout=5000)
                    log["steps"].append("click_cn_tab")
                    page.wait_for_timeout(3000)
        except Exception as exc:
            log["cn_tab_warning"] = str(exc)

        # 点 POI — 优先精确匹配，失败则模糊匹配；都失败 → 抛错
        poi_exact = page.locator('div.name-QUhTI4').filter(has_text=location).first
        try:
            poi_exact.scroll_into_view_if_needed(timeout=8000)
            poi_exact.click(timeout=5000)
            log["selected"] = location
            log["steps"].append("select_poi_exact")
        except Exception as exc:
            log["poi_exact_warning"] = str(exc)
            # 模糊兜底
            opt = page.get_by_text(location, exact=False).first
            if opt.count() > 0:
                opt.click()
                log["selected"] = location
                log["steps"].append("select_poi_partial")
            else:
                # 兜底都失败 → 直接当失败
                raise LocationNotFoundError(
                    f"门店名称 '{location}' 在抖音 POI 搜索结果中未找到"
                ) from exc

        # 关弹层
        for _ in range(3):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.wait_for_timeout(800)
    except Exception as exc:
        # LocationNotFoundError 直接抛出（让上层 _actions 写入 online_id_holder["error"]）
        if isinstance(exc, LocationNotFoundError):
            raise
        log["warning"] = f"位置设置异常: {exc}"
    return log


class LocationNotFoundError(Exception):
    """POI 搜索未命中 — 立即终止本次发布（不让其带着无 POI 状态提交）。"""
    pass


def set_schedule(page, when: str) -> dict:
    """设置定时发布。when 格式 'yyyy-MM-dd HH:mm[:ss]' 或 'now'。"""
    log: dict = {"steps": [], "scheduled_at": when}
    if when.lower() in ("", "now", "立即", "立即发布"):
        log["skipped"] = True
        return log

    # 切到"定时发布"
    try:
        clicked = page.evaluate("""() => {
            for (const lb of document.querySelectorAll('label')) {
                if (lb.innerText && lb.innerText.trim() === '定时发布') { lb.click(); return 'label-click'; }
            }
            for (const inp of document.querySelectorAll('input[type=radio]')) {
                const txt = inp.parentElement ? inp.parentElement.innerText : '';
                if (txt.includes('定时发布')) { inp.click(); return 'input-click'; }
            }
            return null;
        }""")
        if clicked:
            log["steps"].append(f"radio_{clicked}")
            page.wait_for_timeout(2000)
    except Exception as exc:
        log["warning"] = f"radio 切换失败: {exc}"

    # 填日期
    try:
        for _ in range(3):
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

        time_input = page.locator('input[placeholder="日期和时间"]').first
        if time_input.count() == 0:
            time_input = page.locator('input.semi-input-default').filter(has_text="").nth(0)
        time_input.wait_for(state="visible", timeout=5000)

        # #bugfix：plan_time 可能是 "yyyy-MM-dd HH:MM:SS"（含秒）或 "yyyy-MM-dd HH:MM"（已裁秒）。
        # 抖音 input 接受 "yyyy-MM-dd HH:MM"，所以只在含秒时才删秒，不要把分钟也削掉。
        if re.search(r":\d{2}:\d{2}$", when):
            when_short = re.sub(r":\d{2}$", "", when)  # 删 ":SS"
        else:
            when_short = when  # 已是 "yyyy-MM-dd HH:MM"
        set_res = page.evaluate(
            """(val) => {
                for (const inp of document.querySelectorAll('input')) {
                    if (inp.placeholder === '日期和时间') {
                        const proto = Object.getPrototypeOf(inp);
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        setter.call(inp, val);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        return { ok: true, after: inp.value };
                    }
                }
                return { ok: false };
            }""",
            when_short,
        )
        log["steps"].append("set_value")
        log["set_result"] = set_res
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.wait_for_timeout(500)
    except Exception as exc:
        log["error"] = str(exc)
        log["warning"] = "定时外层 fill 失败，需人工确认"
    return log


def set_allow_save(page, allow: Optional[bool]) -> dict:
    """设置「保存权限」（允许/不允许）。allow=None 时不动。"""
    log: dict = {"steps": [], "target": allow, "action": None}
    if allow is None:
        log["skipped"] = True
        return log

    try:
        info = page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('div, span')) {
                    const t = (el.innerText || '').trim();
                    if (t !== '保存权限') continue;
                    let scope = el;
                    for (let i = 0; i < 6 && scope; i++) {
                        const lbls = scope.querySelectorAll('label');
                        if (lbls.length >= 2) {
                            const opts = [...lbls].map(l => (l.innerText || '').trim());
                            const inputs = scope.querySelectorAll('input[type="radio"]');
                            const curIdx = [...inputs].findIndex(i => i.checked);
                            return {ok: true, opts, curIdx};
                        }
                        scope = scope.parentElement;
                    }
                }
                return {ok: false};
            }"""
        )
        if not info.get("ok"):
            log["warning"] = "未找到「保存权限」radio 组"
            return log
        target_idx = 0 if allow else 1
        if info["curIdx"] == target_idx:
            log["action"] = "noop"
            return log
        clicked = page.evaluate(
            """(idx) => {
                for (const el of document.querySelectorAll('div, span')) {
                    if ((el.innerText || '').trim() !== '保存权限') continue;
                    let scope = el;
                    for (let i = 0; i < 6 && scope; i++) {
                        const lbls = scope.querySelectorAll('label');
                        if (lbls.length >= 2) { lbls[idx].click(); return true; }
                        scope = scope.parentElement;
                    }
                }
                return false;
            }""",
            target_idx,
        )
        if clicked:
            log["action"] = "set_allow" if allow else "set_disallow"
            log["selected"] = info["opts"][target_idx]
        else:
            log["warning"] = "click 失败"
        page.wait_for_timeout(800)
    except Exception as exc:
        log["warning"] = f"异常: {exc}"
    return log


def click_publish_and_capture(page, mode: str, network_log: list,
                                timeout_s: int = 60,
                                risk_wait_seconds: int = 300) -> Optional[str]:
    """点发布按钮 + 拦截 create_v2 响应拿 item_id。

    参数:
        page: Playwright page
        mode: 'schedule' / 'now'
        network_log: 共享 list，外部 _on_response 会 append 记录
        timeout_s: 初次等待 item_id 的秒数
        risk_wait_seconds: 检测到二次验证后最长等待秒数（默认 5 分钟，够用户收短信）

    返回:
        online_video_id（item_id 数字串） / None

    #bugfix：create_v2 响应 body 即使含 item_id，也可能 status_code != 0（如 -2158 次数上限），
    此时 _ID_RE 会匹到 ID 但实际是失败。必须在解析 ID 之前先判 status_code，非 0 一律返 None
    并通过 log_extra 透传给上层（_actions 写 online_id_holder["error"]）。
    """
    log_extra = {"mode": mode, "clicked": None, "url_at": page.url}
    # URL 守卫
    if not any(seg in page.url for seg in ("/upload", "/post/video", "/post/photo")):
        log_extra["aborted"] = True
        log_extra["warning"] = f"URL 不在上传编辑页（{page.url}）"
        logger.warning(f"[publish] 守卫拦截：URL={page.url}")
        return None

    # schedule 模式防御
    if mode == "schedule":
        check = page.evaluate(
            """() => {
                const inp = document.querySelector('input[placeholder="日期和时间"]');
                return {dt: inp ? inp.value : ''};
            }"""
        )
        if not check.get("dt"):
            log_extra["aborted"] = True
            log_extra["warning"] = f"schedule 模式但 datetime 为空"
            return None

    # 记录 click 前的长度
    baseline = len(network_log)
    # 定位发布按钮
    popover_btn = page.locator('#popover-tip-container button:has-text("发布")')
    popover_count = popover_btn.count()
    target_locator = None
    if popover_count > 0:
        target_locator = popover_btn.first
        click_method = "popover-tip-container"
    else:
        # 兜底：role=button name=发布
        btn = page.get_by_role("button", name="发布").first
        if btn.count() > 0:
            target_locator = btn
            click_method = "by_role"
    if target_locator is None:
        log_extra["warning"] = "未找到发布按钮"
        return None

    target_locator.click()
    log_extra["clicked"] = f"发布 ({click_method})"
    logger.info(f"[publish] 点击: 发布（{click_method}, mode={mode}）")

    # 等 create_v2 响应里 item_id（最多 60s）；二次验证可大幅延长
    deadline_s = timeout_s
    polled = 0
    while polled < deadline_s:
        for rec in network_log[baseline:]:
            if "aweme/create_v2" not in rec.get("url", ""):
                continue
            # #bugfix：403 = 风控立即识别，跳过死等超时；触发二次验证等待
            if rec.get("status") == 403:
                log_extra["create_v2_403"] = True
                logger.warning("[publish] create_v2 返 403（疑似风控），转入二次验证等待")
                if risk_wait_seconds > 0:
                    online_id = wait_for_verify_or_success(
                        page, network_log, baseline, timeout_s=risk_wait_seconds,
                    )
                    if online_id:
                        return online_id
                return None
            # #bugfix：业务失败判定走双判 status_code + status_msg 关键字
            # 优先用 _on_response 预解析字段（避免 re 扫描整个 body）
            sc_info = None
            if rec.get("sc_status_code") is not None:
                sc_info = {"status_code": rec["sc_status_code"],
                           "status_msg": rec.get("sc_status_msg") or ""}
            else:
                sc_info = _extract_status_code(rec.get("body_full", ""))
            if _is_business_failed(sc_info):
                log_extra["create_v2_business_fail"] = sc_info
                logger.warning(
                    f"[publish] create_v2 业务失败 status_code={sc_info['status_code']} "
                    f"status_msg={sc_info.get('status_msg', '')[:120]}"
                )
                return None
            body = rec.get("body_full", "")
            m = _ID_RE.search(body)
            if m:
                online_id = m.group(2)
                logger.info(f"[publish] create_v2 OK → {m.group(1)}={online_id}")
                # 处理弹层「确认发布」
                page.wait_for_timeout(2000)
                for d_txt in ["确认发布", "确认", "立即发布", "我已知晓"]:
                    try:
                        d = page.locator(f'button:has-text("{d_txt}")').first
                        if d.count() > 0 and d.is_visible():
                            d.click()
                            logger.info(f"[publish] 弹窗选: {d_txt}")
                            page.wait_for_timeout(3000)
                            break
                    except Exception:
                        continue
                return online_id
        page.wait_for_timeout(1000)
        polled += 1

    log_extra["create_v2_timeout"] = True
    # 初次超时：可能是二次验证触发，转入「等用户验证」分支
    if risk_wait_seconds > 0 and _has_verify_mask(page):
        logger.warning(f"[publish] 检测到二次验证 mask，转入用户手动验证等待（最长 {risk_wait_seconds}s）")
        online_id = wait_for_verify_or_success(
            page, network_log, baseline, timeout_s=risk_wait_seconds,
        )
        if online_id:
            logger.info(f"[publish] 验证通过拿到 item_id={online_id}")
            return online_id
        return None

    # fallback：日志里扫一遍
    for rec in network_log[baseline:]:
        if "aweme/create_v2" in rec.get("url", ""):
            sc_info = None
            if rec.get("sc_status_code") is not None:
                sc_info = {"status_code": rec["sc_status_code"],
                           "status_msg": rec.get("sc_status_msg") or ""}
            else:
                sc_info = _extract_status_code(rec.get("body_full", ""))
            if _is_business_failed(sc_info):
                continue  # 业务失败，跳过
            m = _ID_RE.search(rec.get("body_full", ""))
            if m:
                logger.info(f"[publish] fallback 拿到 {m.group(1)}={m.group(2)}")
                return m.group(2)
    logger.warning(f"[publish] {deadline_s}s 内未拿到 create_v2 item_id")
    return None


# 状态码解析（create_v2 响应体顶层结构）
_SC_RE = re.compile(
    r'"status_code"\s*:\s*(-?\d+)'
    r'(?:\s*,\s*"status_msg"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)")?'
)

# 抖音 status_code=0 但 status_msg 含错误关键字的兜底集合
# 实测部分接口（上传/审核类）用 status_code=0 表示 HTTP 成功但业务失败。
# 严选完整短语，避免「无错误」「上传成功」等正常 status_msg 被误伤。
_FAIL_MSG_KEYWORDS = (
    "次数已达到上限",
    "投稿次数已达上限",
    "已达上限",
    "操作失败",
    "发布失败",
    "提交失败",
    "上传失败",
    "审核未通过",
    "内容违规",
    "视频违规",
    "不允许发布",
    "暂不可用",
    "请稍后再试",
    "账号被封禁",
    "账号异常",
)


def _extract_status_code(body: str) -> Optional[dict]:
    """从 create_v2 响应 body 解析顶层 status_code / status_msg。

    解析失败返回 None（让上层走正常成功路径）。
    成功解析无论 status_code 是 0 还是非 0 都返回 dict，便于上层双判。
    """
    if not body:
        return None
    try:
        m = _SC_RE.search(body)
        if not m:
            return None
        sc = int(m.group(1))
        msg = (m.group(2) or "").replace('\\"', '"').replace('\\\\', '\\')
        return {"status_code": sc, "status_msg": msg}
    except (ValueError, IndexError):
        return None


def _is_business_failed(sc_info: Optional[dict]) -> bool:
    """判定 create_v2 是否业务失败——双判 status_code + status_msg 关键字。"""
    if not sc_info:
        return False
    if sc_info["status_code"] != 0:
        return True
    msg = sc_info.get("status_msg") or ""
    return any(kw in msg for kw in _FAIL_MSG_KEYWORDS)


# ---------- 二次验证（短信/滑块）等待 ----------

def _has_verify_mask(page) -> bool:
    """检测抖音二次验证界面（短信/滑块）是否出现。"""
    return page.evaluate(
        """() => {
            const sels = [
                '#uc-second-verify',
                '.second-verify-mask',
                '[class*="verify-mask"]',
                '[class*="VerifyMask"]',
                '[class*="captcha"]',
                '[class*="Captcha"]',
                '[class*="slider"]',
                '[class*="Slider"]',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) return true;
            }
            // 兜底：检测 body 文本含「短信验证码 / 滑动验证 / 人脸验证」
            const txt = (document.body.innerText || '');
            if (/短信验证码|滑动验证|人脸验证|二次验证|安全验证/.test(txt)) {
                // 必须有可见的 input 才算真验证
                for (const inp of document.querySelectorAll('input')) {
                    if (inp.offsetParent !== null) return true;
                }
            }
            return false;
        }"""
    )


def wait_for_verify_or_success(
    page,
    network_log: list,
    baseline_idx: int,
    timeout_s: int = 300,
) -> Optional[str]:
    """等 create_v2 拿到 item_id，或检测到二次验证 mask 卡住等用户操作。

    流程：
    - 每秒轮询 create_v2 响应里是否含 item_id
    - 若检测到二次验证 mask（短信/滑块），暂停等用户完成验证
    - 用户在浏览器中输入验证码后，页面会自动重新发请求；继续拦截 create_v2

    参数:
        page: Playwright page（必须 headed，verify 弹窗要肉眼可见）
        network_log: 共享 list
        baseline_idx: 本次发布相关响应的起点 index
        timeout_s: 总等待秒数

    返回:
        item_id / None（超时）
    """
    deadline = time.time() + timeout_s
    verify_started_at = None
    last_log_ts = 0
    while time.time() < deadline:
        # 1) 优先看有没有 item_id
        for rec in network_log[baseline_idx:]:
            if "aweme/create_v2" not in rec.get("url", ""):
                continue
            body = rec.get("body_full", "")
            m = _ID_RE.search(body)
            if m:
                return m.group(2)

        # 2) 看是不是触发了二次验证
        if _has_verify_mask(page):
            now = time.time()
            # 30s 内只打一次日志（避免刷屏）
            if verify_started_at is None:
                verify_started_at = now
                logger.warning(
                    "\n[verify] 检测到抖音二次验证界面（短信/滑块）\n"
                    f"[verify] 请在弹出的浏览器里完成验证（最多等 {int(deadline - now)}s）\n"
                    f"[verify] 验证完成后脚本会自动继续（无需手动操作）"
                )
            elif now - last_log_ts > 30:
                remaining = int(deadline - now)
                logger.info(f"[verify] 仍在等待二次验证...（剩余 {remaining}s）")
                last_log_ts = now
            page.wait_for_timeout(1000)
            continue

        # 3) 都没有 → 普通轮询
        page.wait_for_timeout(1000)

    logger.warning(f"[wait_for_verify_or_success] 超时（{timeout_s}s），未拿到 item_id")
    return None


# ---------- 主流程：sync_publish_video ----------

def _extract_id_from_text(text: str) -> Optional[str]:
    m = _ID_RE.search(text)
    return m.group(2) if m else None


def sync_publish_video(
    *,
    profile_dir: Path,
    video_path: Path,
    title: str,
    declaration: str,
    location: str,
    schedule: str = "",
    allow_save: Optional[bool] = None,
    account_dir: Optional[Path] = None,
    headless: Optional[bool] = None,
    risk_wait_seconds: int = 180,
    reuse_browser_actor: bool = True,
) -> dict:
    """同步版发布视频到抖音：打开创作中心 → 上传 → 填字段 → 点发布 → 拿 item_id。

    参数:
        #452：profile_dir 持久化 chromium profile（cookies + localStorage + IndexedDB）
        profile_dir: chromium 持久化 profile 目录（H:\ShortVideoToolData\accounts\<id>\profile\）
        video_path: 视频绝对路径
        title: 作品简介（含话题 #xxx）
        declaration: 自主声明文案（如"无需添加自主声明"）
        location: POI 门店名（精确匹配 .name-QUhTI4）
        schedule: 定时发布时间 yyyy-MM-dd HH:mm[:ss]；空=立即发布
        allow_save: True/False/None
        account_dir: 截图输出目录（默认 H:/ShortVideoToolData/log/publish_<ts>）
        headless: True/False/None（None 时读 settings.browser_show_window）
        risk_wait_seconds: 触发风控时最大等待秒数
        reuse_browser_actor: True 用全局 browser_actor；False 独立起 browser（独立实例适合登录窗场景）

    返回:
        {"success": bool, "online_video_id": str|None, "message": str}
    """
    from app.services.setting_service import load_settings

    if account_dir is None:
        from app.db import get_db
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            d = get_db()
            r = d.query_one("SELECT name FROM sqlite_master WHERE type='table'")  # 仅触发校验
        except Exception:
            pass
        # #506：原硬编码 H:/ShortVideoToolData/log 是 bug，改用 get_data_dir()/log 跟随配置
        from app.services.setting_service import get_data_dir
        account_dir = get_data_dir() / "log" / f"publish_{ts}"

    account_dir.mkdir(parents=True, exist_ok=True)

    # 解析 headless：完全尊重系统配置 browser_show_window。
    # 之前 risk_wait_seconds > 0 时强制 headed 覆盖了用户配置，与「跟系统配置」原则冲突；
    # 用户选择「A. 完全尊重 setting」，风控期弹窗不可见由用户自行权衡（失败可手动重试）。
    if headless is None:
        try:
            headless = not load_settings().get("browser_show_window", False)
        except Exception:
            headless = True

    if reuse_browser_actor:
        return _publish_via_browser_actor(
            profile_dir, video_path, title, declaration, location,
            schedule, allow_save, account_dir, headless, risk_wait_seconds,
        )

    # 独立 browser 路径（登录窗场景）
    return _publish_via_independent_browser(
        profile_dir, video_path, title, declaration, location,
        schedule, allow_save, account_dir, headless, risk_wait_seconds,
    )


def _publish_via_browser_actor(
    profile_dir, video_path, title, declaration, location,
    schedule, allow_save, account_dir, headless, risk_wait_seconds,
) -> dict:
    """#452：走全局 browser_actor + launch_persistent_context(user_data_dir=profile_dir)。

    复用现有 chromium + 反自动化注入；登录态走持久化 profile（IndexedDB 完整保留）。
    """
    from app.core.douyin.browser import browser_actor, UA, _ANTI_BOT_INIT_SCRIPT

    network_log: list = []
    online_id_holder: dict = {}

    def _on_response(resp):
        try:
            req = resp.request
            url = resp.url
            if "douyin.com" not in url:
                return
            if req.method not in ("POST", "PUT"):
                return
            if not any(k in url for k in ["/create/", "/publish/", "/post/", "/upload/finish",
                                            "/commit/", "/aweme/", "/creator-micro/data/"]):
                return
            text = ""
            for _ in range(2):
                try:
                    text = resp.text()
                    break
                except Exception:
                    try:
                        body = resp.body()
                        text = body.decode("utf-8", errors="ignore")
                        break
                    except Exception:
                        time.sleep(1)
            # #bugfix：保留 body 末尾 2KB 避免截断丢失尾部 status_code/status_msg
            # status_code 通常在尾部；抖音偶发在 body 末尾加 error 堆栈 / extra 字段
            if len(text) < 8000:
                body_full = text
            else:
                body_full = text[:6000] + "\n...[truncated]...\n" + text[-2000:]
            # 预解析 status_code/msg 存到独立字段，避免上层重新正则扫描整个 body
            sc_info = _extract_status_code(body_full)
            rec = {
                "url": url[:200],
                "method": req.method,
                "status": resp.status,
                "body_len": len(text),
                "body_full": body_full,
                # 缓存解析结果，click_publish_and_capture 直接读字段
                "sc_status_code": sc_info["status_code"] if sc_info else None,
                "sc_status_msg": sc_info["status_msg"] if sc_info else None,
            }
            network_log.append(rec)
        except Exception:
            pass

    def _actions(page):
        """browser_actor.run 的 actions 回调：完整发布流程。"""
        try:
            # load 比 domcontentloaded 慢 2-5s；60s 充裕覆盖发布页重资源（上传组件 + 富文本 + 草稿）。
page.goto(UPLOAD_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)
            safe_screenshot(page, "step2_enter_upload", account_dir)

            # 关残留 banner
            dismiss_unfinished_banner(page, account_dir)
            page.wait_for_timeout(1000)

            # 选文件
            file_input = page.locator('input[type="file"]').first
            try:
                file_input.wait_for(state="attached", timeout=20000)
                file_input.set_input_files(str(video_path))
                logger.info(f"[upload] 提交文件: {video_path}")
            except Exception as exc:
                logger.error(f"[err] 找不到文件输入框: {exc}")
                safe_screenshot(page, "step2_no_file_input", account_dir)
                online_id_holder["error"] = "no_file_input"
                return

            # 等上传
            uploaded = wait_upload_done(page, timeout=300)
            if not uploaded:
                online_id_holder["error"] = "upload_timeout"
                return
            safe_screenshot(page, "step2_after_upload", account_dir)

            # 填字段
            fill_title(page, title)
            page.wait_for_timeout(1500)
            set_self_declaration(page, declaration)
            page.wait_for_timeout(1500)
            # #bugfix：门店查不到时 set_location 直接抛 LocationNotFoundError
            # 这里捕获后立即终止后续发布（不点发布按钮），避免带无 POI 状态提交
            if location:
                try:
                    set_location(page, location)
                except LocationNotFoundError as loc_exc:
                    safe_screenshot(page, "step2_location_not_found", account_dir)
                    logger.error(f"[publish] 门店查不到: {loc_exc}")
                    online_id_holder["error"] = f"门店查不到：{loc_exc}"
                    return
                page.wait_for_timeout(2000)
            set_schedule(page, schedule)
            page.wait_for_timeout(1500)
            set_allow_save(page, allow_save)
            page.wait_for_timeout(2000)
            safe_screenshot(page, "step2_after_fill", account_dir)

            # 点发布
            mode = "schedule" if schedule and schedule.lower() not in ("", "now", "立即") else "now"
            # #bugfix：baseline 必须在 click_publish_and_capture 调用前算好（click 内部同名变量不可见）
            baseline = len(network_log)
            online_id = click_publish_and_capture(page, mode, network_log,
                                                    timeout_s=60,
                                                    risk_wait_seconds=risk_wait_seconds)
            if online_id:
                online_id_holder["id"] = online_id
            else:
                # #bugfix：click_publish_and_capture 返 None 时区分业务失败 vs 超时/无响应
                # 扫一遍网络日志，命中 create_v2 业务失败则把 status_msg 写到 error 字段
                for rec in network_log[baseline:]:
                    if "aweme/create_v2" not in rec.get("url", ""):
                        continue
                    sc_info = None
                    if rec.get("sc_status_code") is not None:
                        sc_info = {"status_code": rec["sc_status_code"],
                                   "status_msg": rec.get("sc_status_msg") or ""}
                    else:
                        sc_info = _extract_status_code(rec.get("body_full", ""))
                    if _is_business_failed(sc_info):
                        online_id_holder["error"] = (
                            f"抖音拒绝发布：status_code={sc_info['status_code']} "
                            f"msg={sc_info.get('status_msg', '')[:200]}"
                        )
                        break
            page.wait_for_timeout(4000)
            safe_screenshot(page, "step2_after_publish", account_dir)
            # #452：launch_persistent_context 关闭时自动 flush profile_dir（cookies + IndexedDB 落盘），
            # 无需手动 save_storage
        except Exception as exc:
            logger.exception(f"[publish] 流程异常: {exc}")
            online_id_holder["error"] = str(exc)

    try:
        # #452：launch_persistent_context(user_data_dir=profile_dir)，登录态走持久化 profile
        # 不走 browser_actor 的 new_context（new_context 不支持 user_data_dir 复用）
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=AutomationControlled,AutomationControlledForRenderProcessHost,AutomationControlledForSwap",
                    "--no-sandbox",
                    "--start-maximized",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                page = context.new_page()
                page.add_init_script(_ANTI_BOT_INIT_SCRIPT)
                page.on("response", _on_response)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                _actions(page)
            finally:
                context.close()  # flush profile 落盘（更新 cookies / IndexedDB）
    except Exception as exc:
        logger.exception(f"[publish] 浏览器调用异常: {exc}")
        return {"success": False, "online_video_id": None,
                "message": f"浏览器异常：{exc}"}

    if "error" in online_id_holder:
        return {"success": False, "online_video_id": None,
                "message": online_id_holder["error"]}
    if "id" in online_id_holder:
        return {"success": True, "online_video_id": online_id_holder["id"],
                "message": "发布成功"}
    return {"success": False, "online_video_id": None,
            "message": "未在 60s 内拦截到 create_v2 item_id（疑似风控或验证）"}


def _publish_via_independent_browser(
    profile_dir, video_path, title, declaration, location,
    schedule, allow_save, account_dir, headless, risk_wait_seconds,
) -> dict:
    """#452：独立 chromium runtime + launch_persistent_context(user_data_dir=profile_dir)。

    登录态走持久化 profile；不用 sync_playwright + new_context（漏 IndexedDB）。
    """
    from playwright.sync_api import sync_playwright
    from app.core.douyin.browser import UA, _ANTI_BOT_INIT_SCRIPT

    network_log: list = []
    online_id_holder: dict = {}

    def _on_response(resp):
        try:
            req = resp.request
            url = resp.url
            if "douyin.com" not in url:
                return
            if req.method not in ("POST", "PUT"):
                return
            if not any(k in url for k in ["/create/", "/publish/", "/post/", "/upload/finish",
                                            "/commit/", "/aweme/", "/creator-micro/data/"]):
                return
            text = ""
            for _ in range(2):
                try:
                    text = resp.text()
                    break
                except Exception:
                    try:
                        body = resp.body()
                        text = body.decode("utf-8", errors="ignore")
                        break
                    except Exception:
                        time.sleep(1)
            if len(text) < 8000:
                body_full = text
            else:
                body_full = text[:6000] + "\n...[truncated]...\n" + text[-2000:]
            sc_info = _extract_status_code(body_full)
            network_log.append({
                "url": url[:200],
                "method": req.method,
                "status": resp.status,
                "body_len": len(text),
                "body_full": body_full,
                "sc_status_code": sc_info["status_code"] if sc_info else None,
                "sc_status_msg": sc_info["status_msg"] if sc_info else None,
            })
        except Exception:
            pass

    def _actions(page):
        try:
            # load 比 domcontentloaded 慢 2-5s；60s 充裕覆盖发布页重资源（上传组件 + 富文本 + 草稿）。
page.goto(UPLOAD_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)
            safe_screenshot(page, "step2_enter_upload", account_dir)
            dismiss_unfinished_banner(page, account_dir)
            page.wait_for_timeout(1000)
            file_input = page.locator('input[type="file"]').first
            try:
                file_input.wait_for(state="attached", timeout=20000)
                file_input.set_input_files(str(video_path))
            except Exception as exc:
                logger.error(f"[err] 找不到文件输入框: {exc}")
                online_id_holder["error"] = "no_file_input"
                return
            uploaded = wait_upload_done(page, timeout=300)
            if not uploaded:
                online_id_holder["error"] = "upload_timeout"
                return
            safe_screenshot(page, "step2_after_upload", account_dir)
            fill_title(page, title)
            page.wait_for_timeout(1500)
            set_self_declaration(page, declaration)
            page.wait_for_timeout(1500)
            # #bugfix：门店查不到立即终止，避免带无 POI 状态提交
            if location:
                try:
                    set_location(page, location)
                except LocationNotFoundError as loc_exc:
                    safe_screenshot(page, "step2_location_not_found", account_dir)
                    logger.error(f"[publish] 门店查不到: {loc_exc}")
                    online_id_holder["error"] = f"门店查不到：{loc_exc}"
                    return
                page.wait_for_timeout(2000)
            set_schedule(page, schedule)
            page.wait_for_timeout(1500)
            set_allow_save(page, allow_save)
            page.wait_for_timeout(2000)
            safe_screenshot(page, "step2_after_fill", account_dir)
            mode = "schedule" if schedule and schedule.lower() not in ("", "now", "立即") else "now"
            # #bugfix：baseline 必须在 click_publish_and_capture 调用前算好（click 内部同名变量不可见）
            baseline = len(network_log)
            online_id = click_publish_and_capture(page, mode, network_log,
                                                    timeout_s=60,
                                                    risk_wait_seconds=risk_wait_seconds)
            if online_id:
                online_id_holder["id"] = online_id
            else:
                # #bugfix：区分业务失败 vs 超时/无响应
                for rec in network_log[baseline:]:
                    if "aweme/create_v2" not in rec.get("url", ""):
                        continue
                    sc_info = None
                    if rec.get("sc_status_code") is not None:
                        sc_info = {"status_code": rec["sc_status_code"],
                                   "status_msg": rec.get("sc_status_msg") or ""}
                    else:
                        sc_info = _extract_status_code(rec.get("body_full", ""))
                    if _is_business_failed(sc_info):
                        online_id_holder["error"] = (
                            f"抖音拒绝发布：status_code={sc_info['status_code']} "
                            f"msg={sc_info.get('status_msg', '')[:200]}"
                        )
                        break
            page.wait_for_timeout(4000)
            safe_screenshot(page, "step2_after_publish", account_dir)
            # #452：launch_persistent_context 关闭时自动 flush profile_dir
        except Exception as exc:
            logger.exception(f"[publish] 流程异常: {exc}")
            online_id_holder["error"] = str(exc)

    with sync_playwright() as pw:
        # #452：launch_persistent_context(user_data_dir=profile_dir)
        # 复用已登录的 chromium profile（cookies + localStorage + IndexedDB 完整保留）
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            # #451：抖音 2025+ 指纹检测升级，launch args 加 disable-features 关掉 AutomationControlled
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled,AutomationControlledForRenderProcessHost,AutomationControlledForSwap",
                "--no-sandbox",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            try:
                page = context.new_page()
                page.add_init_script(_ANTI_BOT_INIT_SCRIPT)
                page.on("response", _on_response)
                # 抢焦点到桌面最前
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                _actions(page)
            finally:
                context.close()  # #452：flush profile（cookies/IndexedDB 落盘）
        except Exception as exc:
            logger.exception(f"[publish] 流程异常: {exc}")
            online_id_holder["error"] = str(exc)

    if "error" in online_id_holder:
        return {"success": False, "online_video_id": None,
                "message": online_id_holder["error"]}
    if "id" in online_id_holder:
        return {"success": True, "online_video_id": online_id_holder["id"],
                "message": "发布成功"}
    return {"success": False, "online_video_id": None,
            "message": "未在 60s 内拦截到 create_v2 item_id（疑似风控或验证）"}