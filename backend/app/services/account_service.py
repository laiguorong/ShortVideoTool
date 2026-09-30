# -*- coding: utf-8 -*-
"""账号服务（F-01）：添加（浏览器登录抓会话）、定时登录态检测、重新登录、启停、编辑、删除。

状态语义（前端展示）：normal=已登录；invalid/undetected=登录失败；disabled=已停用。
手动检测入口已删（UI 无按钮），check_account 仅由定时任务与发布前校验调用。
"""

import json
import shutil
from pathlib import Path

from loguru import logger

from app.core import crypto, notifier
from app.core.douyin import get_douyin_client, LoginInvalidError, DouyinClientError
from app.core.douyin.avatar_cache import cache_avatar, cache_account_avatar
from app.core import task_scheduler
from app.db import get_db
from app.db.utils import now_str
from app.services import setting_service
from app.services.setting_service import get_data_dir
from app.services.douyin_account import get_account_manager
from app.services.task_service import task_service

# 定时检测调度任务标识
_CHECK_JOB_ID = "account_check"


def add_account(cookie: str, remark: str = "",
               profile: dict | None = None,
               profile_dir: str | None = None) -> dict:
    """添加账号：登录窗已抓到 cookie + 页面身份信息 → 加密落库。

    参数:
        cookie: 登录窗抓取的 Cookie 串（必须含 sessionid）
        remark: 备注名（可空，默认取昵称）
        profile: 登录窗 evaluate 拿到的 {"nickname", "douyin_id", "avatar"}（可能字段为空）
        profile_dir: 登录窗临时 chromium profile 目录路径（添加账号场景）
            #fix-profile-dir-move：搬移到 accounts/{id}/profile/，确保后续
            launch_persistent_context 能读到登录态（cookies + IndexedDB）
    返回:
        账号记录 dict
    异常:
        ValueError Cookie 缺 sessionid / 账号已存在
    """
    cookie = cookie.strip()
    if not cookie:
        raise ValueError("Cookie 不能为空")
    # 硬校验：sessionid 必须有（创作者接口鉴权唯一凭据）
    if "sessionid" not in cookie:
        raise ValueError("Cookie 缺少 sessionid，请重新登录")
    p = profile or {}
    douyin_id = (p.get("douyin_id") or "").strip()
    nickname = (p.get("nickname") or "").strip()
    avatar_url = (p.get("avatar") or "").strip()
    d = get_db()
    # 查重（按抖音号；旧记录无 douyin_id 时按 nickname 兜底）
    existing = None
    if douyin_id:
        existing = d.query_one(
            "SELECT id, deleted FROM account WHERE douyin_id=?", (douyin_id,))
    remark_name = remark or nickname
    if existing:
        if not existing["deleted"]:
            raise ValueError(f"账号已存在（抖音号 {douyin_id}）")
        # 曾删除的账号重新登录：复活原记录
        d.update_by_id("account", existing["id"], {
            "nickname": nickname,
            "remark": remark_name,
            "cookie_encrypted": crypto.encrypt(cookie),
            "status": "normal",
            "last_login_time": now_str(),
            "deleted": 0,
        })
        account_id = existing["id"]
    else:
        record = {
            "douyin_id": douyin_id,
            "nickname": nickname,
            "remark": remark_name,
            "cookie_encrypted": crypto.encrypt(cookie),
            "status": "normal",
            "last_login_time": now_str(),
        }
        account_id = d.insert("account", record)
    # 头像下载到账号目录 accounts/<id>/avatar.jpeg（长期保留；失败保留旧文件）
    avatar_local = cache_account_avatar(avatar_url, account_id) if avatar_url else ""
    if avatar_local:
        d.update_by_id("account", account_id, {"avatar": avatar_local})
    row = d.query_one("SELECT * FROM account WHERE id=?", (account_id,))
    row.pop("cookie_encrypted", None)
    # #fix-profile-dir-move：搬移登录窗临时 profile_dir 到账号目录
    # （否则 launch_persistent_context 启动时用空 profile → 无登录态 → 发布失败）
    if profile_dir:
        _move_profile_dir(profile_dir, account_id)
    return row


def _move_profile_dir(src_path: str, account_id: str) -> None:
    """把登录窗临时 chromium profile 目录搬移到 accounts/<id>/profile/。

    失败仅日志（不阻塞账号创建，账号本身可用，仅后续发布需重新登录）。
    """
    from app.services.douyin_account import get_profile_dir
    src = Path(src_path)
    dst = get_profile_dir(account_id)
    if not src.exists():
        logger.warning("[profile-dir-move] 源目录不存在: {}", src)
        return
    if src.resolve() == dst.resolve():
        # 已是目标位置（重新登录场景），无需搬
        return
    try:
        # dst 是 get_profile_dir 已 mkdir 的空目录，先删再 move（避免变成 dst/src_basename/）
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
        logger.info("[profile-dir-move] 临时 profile 搬移成功: {} → {}", src, dst)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[profile-dir-move] 搬移失败: src={} dst={} err={}", src, dst, exc)


def list_accounts(status: str = "", keyword: str = "", page: int = 1, page_size: int = 20) -> dict:
    """账号分页列表。

    参数:
        status: 状态过滤（空=全部）
        keyword: 备注名/昵称/抖音号模糊搜索
    """
    d = get_db()
    where = "WHERE deleted=0"
    params: list = []
    if status:
        where += " AND status=?"
        params.append(status)
    if keyword:
        where += " AND (remark LIKE ? OR nickname LIKE ? OR douyin_id LIKE ?)"
        params += [f"%{keyword}%"] * 3
    result = d.query_page(
        f"SELECT id, douyin_id, nickname, avatar, remark, status, "
        f"last_login_time, fan_count, work_count, create_time FROM account {where} ORDER BY create_time DESC",
        tuple(params), page, page_size)
    # #504：avatar 字段可能指向已被清理/下载失败的本地文件 → 前端 404 显示裂图。
    # 返回前校验文件存在，不存在则置空（前端显示占位首字母头像）。
    data_dir = get_data_dir()
    for row in result.get("list", []):
        av = row.get("avatar") or ""
        if av and not (data_dir / av).is_file():
            row["avatar"] = ""
    return result


def get_cookie(account_id: str) -> str:
    """解密取回账号 Cookie（仅内部使用，接口不外泄）。"""
    d = get_db()
    row = d.query_one("SELECT cookie_encrypted FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row:
        raise ValueError("账号不存在")
    return crypto.decrypt(row["cookie_encrypted"])


def get_extra_cookies(account_id: str) -> dict:
    """取回账号跨域会话（B 方案：buyin 等 SSO 域 Cookie）。

    返回:
        {域关键词: Cookie头串}，无记录返回空 dict
    """
    d = get_db()
    row = d.query_one("SELECT extra_cookies_json FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row or not row["extra_cookies_json"]:
        return {}
    try:
        return json.loads(row["extra_cookies_json"])
    except json.JSONDecodeError:
        return {}


def save_extra_cookies(account_id: str, extra: dict) -> None:
    """保存/合并跨域会话（同域覆盖）。"""
    d = get_db()
    merged = get_extra_cookies(account_id)
    merged.update(extra)
    d.update_by_id("account", account_id, {"extra_cookies_json": json.dumps(merged, ensure_ascii=False)})


def check_account(account_id: str) -> dict:
    """检测单个账号登录态，更新状态。

    时序（统一为 login_window 同源判定，#fix-unify-profile-storage 统一持久化路径）：
    1. 检查 profile_dir（accounts/<id>/profile/）是否有 Cookies 文件 → 无直接 invalid
    2. 用 profile_dir 启 headless Chromium + launch_persistent_context 加载 creator-micro/home
    3. 等 DOM 资料卡 selector [class*="unique_id-"] 出现
    4. 从页面拿 profile（顺路一次 headless 启动完成）
    5. DOM 渲染判定 → normal/invalid
    """
    d = get_db()
    row = d.query_one("SELECT * FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row:
        raise ValueError("账号不存在")

    # 1. profile_dir 不存在或无 Cookies 文件 → 直接 invalid（#fix-unify-profile-storage）
    from app.services.douyin_account import get_profile_dir as _get_profile_dir
    profile_dir = _get_profile_dir(account_id)
    has_cookies = (profile_dir / "Default" / "Network" / "Cookies").exists() or \
                  (profile_dir / "Default" / "Cookies").exists()
    if not has_cookies:
        d.update_by_id("account", account_id, {"status": "invalid", "last_check_time": now_str()})
        _notify_invalid(row, account_id)
        prev_status = row.get("status")
        if prev_status != "invalid":
            logger.info("[check_account] 账号 {} profile 缺失 → invalid",
                        row["remark"] or row["nickname"] or account_id[:8])
        return {"account_id": account_id, "status": "invalid",
                "message": "profile 缺失，请到「账号管理」重新登录"}

    # 2. 用 profile_dir 启 headless + launch_persistent_context 加载主页 + 拿 profile
    from playwright.sync_api import sync_playwright
    from app.core.douyin.browser import _extract_creator_profile, UA, browser_actor

    pw = None
    dom_ok = False
    prof: dict = {"nickname": "", "douyin_id": "", "avatar": ""}
    show_window = bool(setting_service.load_settings().get("browser_show_window", False))
    # 若当前线程已有 BrowserActor tls.runtime，先 cleanup 避免「inside the asyncio loop」
    # 冲突（check_all 调度与拉取任务并发时会复现）。
    browser_actor._reset_tls_for_sync_api()
    try:
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not show_window,
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page.goto("https://creator.douyin.com/creator-micro/home",
                      timeout=30000, wait_until="domcontentloaded")
            # 3. 等 DOM 资料卡 selector（与 login_window 同一 selector）
            for sel in ('[class*="unique_id-"]', '[class*="nick_name"]'):
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    dom_ok = True
                    break
                except Exception:
                    continue
            # 4. DOM 渲染成功才从页面拿 profile
            if dom_ok:
                prof = _extract_creator_profile(page)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("[check_account] headless 检测异常 account={}: {}",
                       account_id[:8], exc)
    finally:
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass

    # 5. 双重确认：DOM 渲染判定（#fix-unify-profile-storage：不再读 storage.json）
    new_status = "normal" if dom_ok else "invalid"
    logger.debug(
        "[check_account] account={} dom_ok={} new_status={}",
        account_id[:8], dom_ok, new_status,
    )

    fields: dict = {"status": new_status, "last_check_time": now_str()}
    # 正常时回填 profile（顺路拿的，不再单独调 _fetch_profile_via_browser）
    if new_status == "normal":
        # #P0-9：profile 空壳（风控/失败）时三个字段全空，跳过回填
        prof_blank = not (prof.get("nickname") or prof.get("avatar") or prof.get("douyin_id"))
        if not prof_blank:
            if prof.get("nickname") and prof["nickname"] != (row.get("nickname") or ""):
                fields["nickname"] = prof["nickname"]
            av = prof.get("avatar") or ""
            if av.startswith("http"):
                av = cache_account_avatar(av, account_id)
            if av and av != (row.get("avatar") or ""):
                fields["avatar"] = av
            if prof.get("douyin_id") and prof["douyin_id"] != (row.get("douyin_id") or ""):
                fields["douyin_id"] = prof["douyin_id"]
            # 浏览器拿不到部分字段时用 meta.json 兜底
            if "nickname" not in fields and "avatar" not in fields:
                mgr = get_account_manager()
                meta = mgr.get(account_id)
                if meta:
                    if "nickname" not in fields and meta.nickname and meta.nickname != (row.get("nickname") or ""):
                        fields["nickname"] = meta.nickname
                    if "avatar" not in fields and meta.avatar_url:
                        mav = meta.avatar_url
                        if mav.startswith("http"):
                            mav = cache_account_avatar(mav, account_id)
                        if mav and mav != (row.get("avatar") or ""):
                            fields["avatar"] = mav
    # #bugfix：扫码覆盖遗留 + storage cookies 错位时，check 拿到的 douyin_id 与其他账号冲突 → UNIQUE 失败 500。
    # 先尝试 update，捕获 IntegrityError 后降级：剔除 douyin_id 字段再 update，保留 status / last_check_time
    import sqlite3
    try:
        d.update_by_id("account", account_id, fields)
    except sqlite3.IntegrityError as exc:
        if "douyin_id" in fields:
            logger.warning(
                "[check_account] douyin_id={!r} 与其他账号冲突，跳过该字段兜底更新 account={}",
                fields.get("douyin_id"), account_id[:8],
            )
            fields.pop("douyin_id", None)
            try:
                d.update_by_id("account", account_id, fields)
            except Exception:
                logger.exception("[check_account] 兜底 update 仍失败 account={}", account_id[:8])
                raise
        else:
            raise

    prev_status = row.get("status")
    if prev_status != new_status:
        logger.info(
            "[check_account] 账号 {} 状态变更 {} → {}",
            row["remark"] or row["nickname"] or account_id[:8],
            prev_status, new_status,
        )
    if new_status == "invalid":
        _notify_invalid(row, account_id)
    else:
        # 恢复正常：联动恢复该账号挂起的发布明细
        try:
            from app.services.publish_service import resume_suspended_items
            resumed = resume_suspended_items(account_id)
            if resumed:
                notifier.notify("info", "account",
                                f"账号 {row['remark'] or row['nickname']} 已恢复",
                                f"{resumed} 条挂起发布明细已恢复待发布")
        except ImportError:
            logger.debug("[account_service] resume_suspended_items 模块未启用，"
                         "跳过挂起明细恢复 account={}", account_id)
    return {
        "account_id": account_id,
        "status": new_status,
        "message": "登录态正常" if new_status == "normal" else "Cookie 失效，请到「账号管理」重新登录",
    }


def _notify_invalid(row: dict, account_id: str) -> None:
    """统一 invalid 通知（check_account storage 缺失 / Cookie 失效 走同一通道）。"""
    notifier.notify("warn", "account",
                    f"账号 {row['remark'] or row['nickname']} 登录态失效",
                    "请到「账号管理」重新登录",
                    action=f"relogin:{account_id}")


def relogin(account_id: str, new_cookie: str, profile: dict | None = None) -> dict:
    """重新登录：更新 Cookie 并恢复状态（用登录窗 evaluate 拿到的身份信息回填）。

    登录态判定：直接读新 cookie 的 sessionid（#fix-unify-profile-storage 不再依赖 storage.json）。
    不调 check_account（避免重开 headless 二次检测——登录窗 headed 已确认 DOM 渲染）。

    校验：
    - 新 Cookie 必须含 sessionid
    - 登录窗拿到的 douyin_id 必须与 DB 原记录一致（防登错账号）
      不一致 → 删除原 profile_dir → 抛 ValueError（前端展示提示）
    - 缺失字段（昵称/头像）保留原值
    """
    d = get_db()
    row = d.query_one("SELECT * FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row:
        raise ValueError("账号不存在")
    new_cookie = new_cookie.strip()
    if "sessionid" not in new_cookie:
        raise ValueError("新 Cookie 缺少 sessionid，请重新登录")
    p = profile or {}
    new_douyin_id = (p.get("douyin_id") or "").strip()
    new_nickname = (p.get("nickname") or "").strip()
    new_avatar = (p.get("avatar") or "").strip()
    # douyin_id 一致性校验（拿到 douyin_id + DB 有 douyin_id → 必须一致；不一致=登错账号）
    if new_douyin_id and row.get("douyin_id") and new_douyin_id != row["douyin_id"]:
        # 删除原 profile_dir：避免下次 launch_persistent_context 误用旧账号会话（#fix-unify-profile-storage）
        from app.services.douyin_account import get_profile_dir as _get_profile_dir
        old_profile_dir = _get_profile_dir(account_id)
        try:
            import shutil as _shutil
            if old_profile_dir.exists():
                _shutil.rmtree(old_profile_dir)
                logger.warning("[relogin] 抖音号不一致，删除原 profile_dir：{}", old_profile_dir)
        except Exception as exc:
            logger.warning("[relogin] 删除旧 profile_dir 失败：{}（不影响主流程）", exc)
        raise ValueError(
            f"登录的账号（抖音号 {new_douyin_id}）与原账号（{row['douyin_id']}）不符，"
            f"原会话已删除，请用原账号重新登录"
        )

    # #fix-unify-profile-storage：登录态判定从 profile_dir 拿（不依赖 storage.json）
    # 登录窗 headed 已确认 DOM 渲染（cookie 含 sessionid 即视为正常；过期由后续 check_account / publish 检测）
    new_status = "normal" if "sessionid" in new_cookie else "invalid"

    fields: dict = {
        "cookie_encrypted": crypto.encrypt(new_cookie),
        "status": new_status,
        "last_login_time": now_str(),
        "last_check_time": now_str(),  # 立即刷新，前端 UI 立刻显示正常
    }
    # profile 用登录窗 headed 已拿到的（不重抓）
    if new_nickname and new_nickname != (row.get("nickname") or ""):
        fields["nickname"] = new_nickname
    # 头像：登录窗拿到就用；拿不到则用 meta.json avatar_url 兜底（与 check_account 一致）
    avatar_url = new_avatar
    if not avatar_url:
        meta = get_account_manager().get(account_id)
        if meta and meta.avatar_url:
            avatar_url = meta.avatar_url
    if avatar_url:
        local = cache_account_avatar(avatar_url, account_id)
        if local and local != (row.get("avatar") or ""):
            fields["avatar"] = local
        elif not local and (row.get("avatar") or "") and not (get_data_dir() / row["avatar"]).is_file():
            # 下载失败且旧头像文件已丢 → 清空，避免前端 404 裂图
            fields["avatar"] = ""
    if new_douyin_id and not row.get("douyin_id"):
        # 旧记录无 douyin_id（早期数据）→ 回填
        fields["douyin_id"] = new_douyin_id
    d.update_by_id("account", account_id, fields)

    if new_status == "invalid":
        # 极端情况：登录窗说成功但 storage 没 sessionid → 通知前端重试
        _notify_invalid(row, account_id)
        return {"account_id": account_id, "status": "invalid",
                "message": "会话保存异常，请重试重新登录"}

    # 恢复正常：联动恢复该账号挂起的发布明细
    try:
        from app.services.publish_service import resume_suspended_items
        resumed = resume_suspended_items(account_id)
        if resumed:
            notifier.notify("info", "account",
                            f"账号 {row['remark'] or row['nickname']} 已恢复",
                            f"{resumed} 条挂起发布明细已恢复待发布")
    except ImportError:
        logger.debug("[relogin] resume_suspended_items 模块未启用，跳过挂起明细恢复 account={}", account_id)
    return {"account_id": account_id, "status": "normal", "message": "重新登录成功"}


def update_remark(account_id: str, remark: str) -> None:
    """编辑备注名。"""
    get_db().update_by_id("account", account_id, {"remark": remark})


def toggle_disable(account_id: str, disabled: bool) -> None:
    """启停账号（停用后不参与发布与数据拉取）。"""
    d = get_db()
    row = d.query_one("SELECT status FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row:
        raise ValueError("账号不存在")
    if disabled:
        d.update_by_id("account", account_id, {"status": "disabled"})
    else:
        # 恢复为未检测状态，等待下轮检测确认
        d.update_by_id("account", account_id, {"status": "undetected"})


def delete_account(account_id: str) -> None:
    """删除账号（逻辑删除 DB + 物理删除账号目录）。

    #276-#fix：原只软删除 DB 行，账号目录 accounts/<id>/storage.json + meta.json
    + 子目录全部残留（cookies 含敏感 session 长期放磁盘是泄漏风险）。
    修复：DB 软删除保留历史 publish_record/stats 外键；物理删目录不留痕。
    头像缓存 cache/avatar/<douyin_id>.jpeg 不删（可能被同一 douyin_id 复活复用）。
    """
    d = get_db()
    row = d.query_one("SELECT remark, nickname FROM account WHERE id=? AND deleted=0", (account_id,))
    if not row:
        raise ValueError("账号不存在")
    d.soft_delete_by_id("account", account_id)
    # 物理删除账号目录（含 storage.json / meta.json / 子目录）
    try:
        from app.services.douyin_account import get_account_manager
        removed = get_account_manager().delete_account(account_id)
        if removed:
            logger.info("[delete_account] 已删除账号目录 {}", account_id)
    except Exception as exc:
        logger.warning("[delete_account] 删除账号目录失败 account={}: {}（DB 已软删除）",
                       account_id, exc)
    notifier.notify("info", "account", f"账号 {row['remark'] or row['nickname']} 已删除",
                    "历史发布记录与统计数据保留")


def check_all() -> int:
    """检测全部未删除且未停用账号（供定时任务与手动触发），返回检测数。"""
    d = get_db()
    rows = d.query_all("SELECT id FROM account WHERE deleted=0 AND status!='disabled'")
    for r in rows:
        check_account(r["id"])
    return len(rows)


# ---------- 定时检测注册 ----------

def register_account_check_job() -> None:
    """按配置注册登录态定时检测（小时级 IntervalTrigger，绕过 10 分钟下限校验）。"""
    from apscheduler.triggers.interval import IntervalTrigger
    hours = int(setting_service.load_settings().get("account_check_hours", 6))
    s = task_scheduler.start_scheduler()
    s.add_job(_timed_check_all, trigger=IntervalTrigger(hours=hours),
              id=_CHECK_JOB_ID, replace_existing=True, max_instances=1, coalesce=True)


def _timed_check_all() -> None:
    """定时回调：直接同步执行（任务队列重构 PR2 #54：账号维护已去掉，不走 task_service）。"""
    check_all()

