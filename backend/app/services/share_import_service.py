# -*- coding: utf-8 -*-
"""分享链接导入任务服务：异步执行、进度落库、失败可重试。

PR4 #57：接入 task_service.submit("download", ...) 统一任务队列。
- 删 _EXECUTOR，改走 task_service.download 池（max_workers=1 防同账号撞 storage）
- type_label 区分视频/音乐（基于首个 share_text 前缀识别）
- progress 统一格式"素材数 X/Y，用时 H:M:S"（实时即终态）
"""

from typing import Optional

from loguru import logger

from app.core.douyin import get_douyin_client
from app.core.douyin.base import DouyinClientError
from app.db import get_db
from app.db.utils import new_id, now_str
from app.services import material_service as ms
from app.services.task_service import _TaskCancelled, _fmt_hms, raise_for_cancel, task_service


def _detect_kind(share_text: str) -> str:
    """视频 / 音乐二选一。

    任务 #59 P0 #9：基于 URL 域名判断而非子串匹配（避免描述里偶然出现
    "music" 字样误判）。抖音音乐域名 `music.douyin.com`/`music.amemv.com`，
    其它含 `v.douyin.com`/无域名为视频。
    """
    import re
    urls = re.findall(r'https?://[^\s]+', share_text or "", flags=re.IGNORECASE)
    for url in urls:
        u = url.lower()
        if "music.douyin.com" in u or "music.amemv.com" in u:
            return "音乐"
    return "视频"


def create_task(category_id: str, share_texts: list[str]) -> str:
    """创建导入任务，立即返回任务 ID。

    参数:
        category_id: 入库分类 ID
        share_texts: 分享文本列表（每行一条）
    返回:
        任务 ID
    """
    d = get_db()
    # 任务 #111：分类必须为视频或音乐类型（决定实体类型入库逻辑）
    # UNCATEGORIZED_ID 作为入库目标允许（虚拟分类）；否则校验存在与类型
    if category_id != ms.UNCATEGORIZED_ID:
        cat = d.query_one(
            "SELECT id, type FROM material_category WHERE id=? AND deleted=0",
            (category_id,))
        if not cat:
            raise ValueError("入库分类不存在")
        if cat["type"] not in ("video", "music"):
            raise ValueError("入库分类类型必须为视频或音乐")

    task_id = new_id()
    texts = [t.strip() for t in share_texts if t.strip()]
    now = now_str()
    d.insert("share_import_task", {
        "id": task_id,
        "category_id": category_id,
        "status": "pending",
        "total": len(texts),
        "success_count": 0,
        "failed_count": 0,
        "message": "等待执行",
        "create_time": now,
        "update_time": now,
    })
    for text in texts:
        d.insert("share_import_item", {
            "id": new_id(),
            "task_id": task_id,
            "share_text": text[:500],
            "status": "pending",
            "message": "",
            "retry_count": 0,
            "create_time": now,
            "update_time": now,
        })
    # PR4 #57：type_label 区分视频/音乐；name 用分类名（share_import_task 无 task_name 列）
    first_text = texts[0] if texts else ""
    type_label = f"素材下载-{_detect_kind(first_text)}"
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (category_id,)) \
        if category_id != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    task_service.submit(
        "download", name, lambda info: _run_task(task_id, info),
        type_label=type_label,
    )
    logger.info("[分享导入] 任务创建 id={} category={} total={}", task_id, category_id, len(texts))
    return task_id


def _process_item(item: dict, category_id: str, client,
                 manual_wait_ms: int = 30000) -> tuple[str, str, Optional[str]]:
    """处理单条分享：返回 (状态, 消息, material_id)。

    任务 #129：三段日志：分享原文 → 解析后链接 → 处理结果。
    任务 #130：单视频处理委托给 _process_single_video（与拉取侧共用）。
    任务 #132：分享链接兼容视频/音乐，按 _source 分流到不同处理函数。
    任务 #111：按入库分类 type 决定实体类型——音乐分类下导入视频链接，
              自动从 video.music 节点提取 BGM 入音乐库（与拉取侧 _download_bgm
              同步入「未分类」逻辑不同：此处入用户选定的分类）。

    四种组合：
    - 分类=music + 视频链接 → 提取 BGM → _process_single_music
    - 分类=music + 音乐链接 → _process_single_music
    - 分类=video + 视频链接 → _process_single_video
    - 分类=video + 音乐链接 → 拒绝（视频分类不该入音乐）
    """
    raw_text = item["share_text"]
    item_id = item.get("id", "?")
    # 1) 原文
    logger.info("[分享导入] 原文 item={} text={!r}", item_id, raw_text)
    try:
        # 任务 #111：按分类 type 决定入库逻辑（音乐 tab 导入视频时提取 BGM）
        d = get_db()
        # UNCATEGORIZED_ID 作为入库目标允许（虚拟分类）
        if category_id == ms.UNCATEGORIZED_ID:
            category_type = "video"  # 默认走视频入库；具体处理时按分享链接类型自动分流
        else:
            cat = d.query_one(
                "SELECT id, type FROM material_category WHERE id=? AND deleted=0",
                (category_id,))
            if not cat:
                raise DouyinClientError("入库分类不存在")
            category_type = cat["type"]

        resolved = client.resolve_share(raw_text, manual_wait_ms=manual_wait_ms)
        source = resolved.get("_source") or ""

        # 2) 解析后的链接（按分类类型 × 实体类型组合分流）
        if category_type == "music" and source == "detail":
            # 音乐分类 + 视频链接 → 提取视频的 BGM 入音乐库（任务 #111）
            music = resolved.get("music") or {}
            if not music.get("download_url"):
                raise DouyinClientError(
                    "视频无可用背景音乐（版权受限或无 BGM），无法导入音乐分类")
            logger.info(
                "[分享导入] 解析后 item={} category=music 实体=video "
                "→ 提取 BGM music_id={} title={!r}",
                item_id,
                music.get("music_id") or "-",
                (music.get("title") or "")[:50],
            )
            result = ms._process_single_music(music, category_id, client, source="share")
        elif source == "music_detail":
            # 音乐分类 + 音乐链接（任务 #132）：直接入音乐库
            if category_type != "music":
                raise DouyinClientError(
                    f"分类类型为 {category_type}，与音乐分享链接不匹配（应在音乐分类下导入）")
            logger.info(
                "[分享导入] 解析后 item={} type=music music_id={} share_url={} title={!r}",
                item_id,
                resolved.get("music_id") or "-",
                resolved.get("share_url") or "-",
                (resolved.get("title") or "")[:50],
            )
            result = ms._process_single_music(resolved, category_id, client, source="share")
        else:
            # 视频分类 + 视频链接（任务 #130）
            if category_type != "video":
                raise DouyinClientError(
                    f"分类类型为 {category_type}，与视频分享链接不匹配（应在视频分类下导入）")
            logger.info(
                "[分享导入] 解析后 item={} type=video video_id={} share_url={} title={!r}",
                item_id,
                resolved.get("video_id") or "-",
                resolved.get("share_url") or "-",
                (resolved.get("title") or "")[:50],
            )
            # 任务 #130：单视频处理统一调 material_service._process_single_video
            # conditions=None → 跳过客户端兜底过滤 + 字幕/人脸过滤 + BGM 同步
            result = ms._process_single_video(
                resolved, category_id, client,
                source="share", conditions=None,
            )
        action = result["action"]
        if action == "new":
            logger.info("[分享导入] 结果 item={} status=success material_id={} title={!r}",
                        item_id, result["material_id"], (result["title"] or "")[:50])
            return "success", result["title"] or "", result["material_id"]
        # filtered / duplicate / failed → 都返回 failed 状态
        logger.info("[分享导入] 结果 item={} status=failed reason={} action={} dup_material_id={}",
                    item_id, result.get("reason"), action,
                    result.get("material_id") or "-")
        return "failed", result.get("reason") or "未知失败", result.get("material_id")
    except DouyinClientError as e:
        msg = str(e)
        logger.warning("[分享导入] 结果 item={} status=failed reason={} text={!r:.60}",
                       item_id, msg, raw_text)
        return "failed", msg, None
    except Exception as e:  # noqa: BLE001
        logger.exception("[分享导入] 未知异常 item={} text={!r:.60}", item_id, raw_text)
        return "failed", str(e), None


def _update_task_summary(task_id: str, total: int) -> None:
    """按当前明细重新计算任务进度并落库。"""
    d = get_db()
    success = d.query_one(
        "SELECT COUNT(*) AS c FROM share_import_item WHERE task_id=? AND status='success' AND deleted=0",
        (task_id,))["c"]
    failed = d.query_one(
        "SELECT COUNT(*) AS c FROM share_import_item WHERE task_id=? AND status='failed' AND deleted=0",
        (task_id,))["c"]
    final_status = "completed" if failed == 0 else "failed"
    msg = f"成功 {success} / 失败 {failed} / 总计 {total}"
    d.execute(
        "UPDATE share_import_task SET status=?, success_count=?, failed_count=?, message=?, update_time=? WHERE id=?",
        (final_status, success, failed, msg, now_str(), task_id))
    logger.info("[分享导入] 任务汇总更新 id={} {}", task_id, msg)


def _run_task(task_id: str, info=None) -> str:
    """后台执行导入任务。

    PR4 #57：接入 task_service 队列，info 透传到 _run_task_inner 更新 progress。
    任务 #59 P0 #4：显式 return 状态机契约对齐。
    #130：worker 抛 _TaskCancelled 时标 share_import_task.status=cancelled 后 re-raise。
    """
    d = get_db()
    d.execute("UPDATE share_import_task SET status=?, update_time=? WHERE id=?",
              ("running", now_str(), task_id))

    client = get_douyin_client()
    items = d.query_all("SELECT * FROM share_import_item WHERE task_id=? AND deleted=0 ORDER BY create_time",
                        (task_id,))
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    category_id = task["category_id"] if task else ""

    # 任务批量重试：把人工等待窗口拉到 60s（覆盖多数验证码耗时）；
    # 首次执行保持默认 30s。
    has_retried = any(i.get("retry_count", 0) > 0 for i in items)
    manual_wait_ms = 60000 if has_retried else 30000
    try:
        _run_task_inner(task_id, items, category_id, client, manual_wait_ms, info)
    except _TaskCancelled:
        # #130：取消时业务表标 cancelled + 剩余 pending item 一并清理，re-raise 让外层置 info.status
        logger.info("[分享导入] 任务取消 id={}", task_id)
        try:
            d.execute(
                "UPDATE share_import_task SET status=?, message=?, update_time=? WHERE id=?",
                ("cancelled", "用户取消", now_str(), task_id))
            d.execute(
                "UPDATE share_import_item SET status='cancelled', message='任务已取消', update_time=? "
                "WHERE task_id=? AND status='pending' AND deleted=0",
                (now_str(), task_id))
            # #P1-4：取消路径刷 success/failed 计数（前端看到的统计是最新真实值）
            _update_task_summary(task_id, len(items))
        except Exception as e:  # noqa: BLE001
            logger.error("[分享导入] 取消清理写 DB 失败：{}", e)
        raise

    # 任务 #59 P0 #4：按 share_import_task 主记录最终状态返回契约
    final = d.query_one("SELECT success_count, failed_count FROM share_import_task WHERE id=?", (task_id,))
    # #92：补任务终态汇总 INFO（之前 _run_task_inner 已 per-item 更新 progress，
    # 但整批完成时无 INFO 收尾，运维查日志看不出"任务几时结束、成功/失败 N 条"）
    task_name = (task or {}).get("task_name", "?") if task else "?"
    if final and final["failed_count"] > 0:
        logger.info(
            "[分享导入] 任务结束 id={} {} 成功 {} 失败 {}",
            task_id[:8], task_name, final["success_count"], final["failed_count"],
        )
        # #92：partial 时把前 3 条失败原因拼到 info.message，前端可直观看到失败明细
        if info and final["success_count"] > 0 and final["failed_count"] > 0:
            failed_items = d.query_all(
                "SELECT share_text, message FROM share_import_item "
                "WHERE task_id=? AND status='failed' AND deleted=0 LIMIT 3",
                (task_id,),
            )
            if failed_items:
                hints = [f"{it['share_text'][:24]}: {it['message'] or '未知'}"
                         for it in failed_items if it.get("message")]
                if hints:
                    info.message = "部分失败：" + "; ".join(hints)
        if final["success_count"] > 0:
            return "partial"
        return "failed"
    logger.info(
        "[分享导入] 任务结束 id={} {} 全部成功 {} 条",
        task_id[:8], task_name, (final or {}).get("success_count", 0),
    )
    return "success"


def _run_task_inner(task_id: str, items: list, category_id: str, client,
                    manual_wait_ms: int = 30000, info=None) -> None:
    """_run_task 的实际处理循环。

    PR4 #57：info 透传，每完成一条 update info.progress。
    """
    d = get_db()
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts（info 可选，无 info 时回退 time.time()）
    start_ts = (info.start_ts if info else None) or time.time()
    total = len(items)
    done = 0
    for item in items:
        # #129：分享导入循环顶部检查取消（每个素材都要拉抖音 API，单条 30s）
        if info:
            raise_for_cancel(info)
        if item["status"] == "success":
            done += 1
            if info:
                info.progress = f"已导入 {done}/{total}，用时 {_fmt_hms(time.time() - start_ts)}"
            continue
        status, msg, material_id = _process_item(item, category_id, client, manual_wait_ms)
        # 首次执行不增加重试次数，仅手动重试时累加
        inc = 0 if item["status"] == "pending" else 1
        d.execute(
            "UPDATE share_import_item SET status=?, message=?, material_id=?, retry_count=retry_count+?, update_time=? WHERE id=?",
            (status, msg, material_id, inc, now_str(), item["id"]))
        done += 1
        # PR3 #56：实时即终态，每条完成即更新进度
        if info:
            info.progress = f"已导入 {done}/{total}，用时 {_fmt_hms(time.time() - start_ts)}"
            if status == "failed":
                # 失败 message 留空（默认），单条失败原因记在 share_import_item.message
                pass
        logger.info("[分享导入] item={} status={} text={!r:.40}", item["id"], status, item["share_text"])

    _update_task_summary(task_id, total)


def _run_single_item(task_id: str, item_id: str, info=None) -> str:
    """后台执行单条重试（手动触发）。

    PR4 #57：接入 task_service.submit("download", ...)——单条重试也走队列，name 用任务名。
    任务 #59 P0 #5：显式 return "success"/"partial"/"failed" 状态机契约对齐。
    """
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts
    start_ts = info.start_ts or time.time()
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        return "failed"
    item = d.query_one(
        "SELECT * FROM share_import_item WHERE id=? AND task_id=? AND deleted=0",
        (item_id, task_id))
    if not item or item["status"] != "pending":
        return "failed"
    client = get_douyin_client()
    # 手动单条重试：人工等待窗口拉长到 60s（应对验证码场景）
    status, msg, material_id = _process_item(item, task["category_id"], client, manual_wait_ms=60000)
    # 重试次数已在标记重试时累加，此处不再增加
    d.execute(
        "UPDATE share_import_item SET status=?, message=?, material_id=?, update_time=? WHERE id=?",
        (status, msg, material_id, now_str(), item_id))
    logger.info("[分享导入] 单条重试 item={} status={} text={!r:.40}", item_id, status, item["share_text"])
    _update_task_summary(task_id, task["total"])
    if info:
        info.progress = f"已导入 1/1，用时 {_fmt_hms(time.time() - start_ts)}"
        if status == "failed":
            info.message = msg
    # 任务 #59 P0 #5：状态机契约对齐
    if status == "failed":
        return "failed"
    if status == "success":
        return "success"
    return "partial"


def get_task(task_id: str) -> Optional[dict]:
    """查询任务主记录。"""
    return get_db().query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))


def list_tasks(page: int = 1, page_size: int = 20) -> dict:
    """分页查询任务列表（含目标分类名）。"""
    return get_db().query_page(
        """SELECT t.*, c.name AS category_name
           FROM share_import_task t
           LEFT JOIN material_category c ON t.category_id=c.id
           WHERE t.deleted=0 ORDER BY t.create_time DESC""",
        (), page, page_size)


def list_items(task_id: str) -> list[dict]:
    """查询任务明细列表。"""
    return get_db().query_all("SELECT * FROM share_import_item WHERE task_id=? AND deleted=0 ORDER BY create_time",
                              (task_id,))


def retry_failed(task_id: str) -> int:
    """重试任务中所有失败的项，返回重试项数。

    仅允许 completed/failed 状态的任务发起重试；running/pending 任务不处理。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("completed", "failed"):
        raise ValueError("任务仍在执行中，无法重试")

    failed_items = d.query_all(
        "SELECT * FROM share_import_item WHERE task_id=? AND status='failed' AND deleted=0",
        (task_id,))
    if not failed_items:
        return 0

    d.execute("UPDATE share_import_task SET status=?, success_count=0, failed_count=0, message=?, update_time=? WHERE id=?",
              ("running", "重试中…", now_str(), task_id))
    # PR4 #57：走 task_service.submit("download", ...)，name 取分类名（同 create_task）
    first_text = failed_items[0].get("share_text", "") if failed_items else ""
    type_label = f"素材下载-{_detect_kind(first_text)}-重试"
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (task["category_id"],)) \
        if task["category_id"] != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    task_service.submit(
        "download", name, lambda info: _run_task(task_id, info),
        type_label=type_label,
    )
    logger.info("[分享导入] 发起重试 id={} count={}", task_id, len(failed_items))
    return len(failed_items)


def retry_item(task_id: str, item_id: str) -> bool:
    """手动重试单条失败项。

    将目标项标记为 pending 并重试次数 +1，后台异步执行。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    item = d.query_one(
        "SELECT * FROM share_import_item WHERE id=? AND task_id=? AND deleted=0",
        (item_id, task_id))
    if not item:
        raise ValueError("明细项不存在")
    if item["status"] != "failed":
        raise ValueError("仅失败项可重试")

    d.execute(
        "UPDATE share_import_item SET status=?, message=?, retry_count=retry_count+1, update_time=? WHERE id=?",
        ("pending", "等待重试", now_str(), item_id))
    d.execute("UPDATE share_import_task SET status=?, message=?, update_time=? WHERE id=?",
              ("running", "重试中…", now_str(), task_id))
    # PR4 #57：单条重试也走 task_service.submit("download", ...)
    first_text = item.get("share_text", "")
    type_label = f"素材下载-{_detect_kind(first_text)}-单条重试"
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (task["category_id"],)) \
        if task["category_id"] != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    task_service.submit(
        "download", name, lambda info: _run_single_item(task_id, item_id, info),
        type_label=type_label,
    )
    logger.info("[分享导入] 单条重试 task={} item={}", task_id, item_id)
    return True


def delete_task(task_id: str) -> None:
    """删除分享导入任务及其明细（软删除）。执行中任务不可删。"""
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] in ("running", "pending"):
        raise ValueError("任务执行中，无法删除")
    now = now_str()
    d.execute("UPDATE share_import_task SET deleted=1, update_time=? WHERE id=?", (now, task_id))
    d.execute("UPDATE share_import_item SET deleted=1, update_time=? WHERE task_id=?", (now, task_id))
    logger.info("[分享导入] 删除任务 id={}", task_id)


def shutdown() -> None:  # noqa: D401  # pragma: no cover
    """应用退出 hook（PR4 #57：share_import 接入 task_service，无独立线程池可关闭）。

    保留函数签名避免调用方修改；task_service.shutdown 由 main 统一调用。
    """
    pass
