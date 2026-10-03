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
from app.services.task_service import (
    TaskInfo, _TaskCancelled, _fmt_hms, raise_for_cancel, task_service)


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


def create_task(category_id: str, share_texts: list[str], type: str) -> str:
    """创建导入任务，立即返回任务 ID。

    参数:
        category_id: 入库分类 ID（UNCATEGORIZED='-' 表示虚拟未分类）
        share_texts: 分享文本列表（每行一条）
        type: 入库类型 video/music（由前端页签决定，与分类解耦）
    返回:
        任务 ID

    校验：非未分类时，category.type 必须等于 type 入参；
    未分类跳过分类校验，type 直接决定入库函数。
    """
    if type not in ("video", "music"):
        raise ValueError("type 必须为 video 或 music")
    # #fix-music-node：未分类是视频/音乐各自共享的虚拟分类节点（categories API 按 type 注入），
    # 允许 UNCATEGORIZED + type=music（与拉取侧自动 BGM 入未分类行为对齐）。
    d = get_db()
    # UNCATEGORIZED_ID 作为入库目标允许（虚拟分类，跳过分类校验）；其他校验存在与 type 一致
    if category_id != ms.UNCATEGORIZED_ID:
        cat = d.query_one(
            "SELECT id, type FROM material_category WHERE id=? AND deleted=0",
            (category_id,))
        if not cat:
            raise ValueError("入库分类不存在")
        if cat["type"] != type:
            raise ValueError(f"入库分类类型为 {cat['type']}，与 type={type} 不匹配")

    task_id = new_id()
    texts = [t.strip() for t in share_texts if t.strip()]
    now = now_str()
    d.insert("share_import_task", {
        "id": task_id,
        "category_id": category_id,
        # #fix-type-param：存 type 供 retry / list_tasks 用（前端页签决定）
        "type": type,
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
    # #fix-music-node：type_label 用 type 入参（前端页签决定），不用 _detect_kind 推
    # （type=music + 视频链接会被 _detect_kind 误判为"视频"）
    type_label = f"素材下载-{'音乐' if type == 'music' else '视频'}"
    first_text = texts[0] if texts else ""
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (category_id,)) \
        if category_id != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    # #fix-type-param：闭包传 type 给 worker（避免 DB 迁移，task 参数已足够）
    task_service.submit(
        "download", name, lambda info: _run_task(task_id, info, type=type),
        type_label=type_label,
    )
    logger.info("[分享导入] 任务创建 id={} category={} total={}", task_id, category_id, len(texts))
    return task_id


def _process_item(item: dict, category_id: str, client,
                 manual_wait_ms: int = 30000, type: str = "video",
                 info: Optional[TaskInfo] = None) -> tuple[str, str, Optional[str]]:
    """处理单条分享：返回 (状态, 消息, material_id)。

    #fix-music-node：分享导入只接视频链接。音乐链接（resolve_share 已抛错）
    被下方 except DouyinClientError 捕获 → failed "仅支持视频链接导入"。

    任务 #130：单视频处理委托给 _process_single_video（与拉取侧共用）。
    #fix-type-param：type 入参直接决定入库类型（前端页签决定），不再从 category_id 推。
    #fix-music-node ingest_mode：
    - type=video + 视频链接 → ingest_mode="video"（正常下载入库；导入不触发 BGM 同步）
    - type=music + 视频链接 → ingest_mode="bgm_only"（只入 BGM，不下视频本体）

    #fix-music-node：info 参数透传，入口处检查取消信号（避免 resolve_share / 下载
    长时间占用期间用户取消无法响应）。
    """
    raw_text = item["share_text"]
    item_id = item.get("id", "?")
    # 0) 取消检查（task_service.raise_for_cancel 抛 _TaskCancelled）
    if info is not None:
        raise_for_cancel(info)
    # 1) 原文
    logger.info("[分享导入] 原文 item={} text={!r}", item_id, raw_text)
    try:
        category_type = type

        # resolve_share 已统一拒绝音乐链接（抛 DouyinClientError → 被下方 except 捕获）
        resolved = client.resolve_share(raw_text, manual_wait_ms=manual_wait_ms)

        # 2) 解析后的链接（按 type 分流 ingest_mode）
        if category_type == "music":
            logger.info(
                "[分享导入] 解析后 item={} type=music video_id={} → ingest_mode=bgm_only 仅入 BGM",
                item_id,
                resolved.get("video_id") or "-",
            )
            result = ms._process_single_video(
                resolved, category_id, client,
                source="share", conditions=None,
                ingest_mode="bgm_only",
            )
        else:
            logger.info(
                "[分享导入] 解析后 item={} type=video video_id={} share_url={} title={!r}",
                item_id,
                resolved.get("video_id") or "-",
                resolved.get("share_url") or "-",
                (resolved.get("title") or "")[:50],
            )
            # 任务 #130：单视频处理统一调 material_service._process_single_video
            # conditions=None → 跳过客户端兜底过滤 + 字幕/人脸过滤 + BGM 同步（导入不触发 BGM 入库）
            result = ms._process_single_video(
                resolved, category_id, client,
                source="share", conditions=None,
                ingest_mode="video",
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


def _run_task(task_id: str, info=None, type: str = "video") -> str:
    """后台执行导入任务。

    PR4 #57：接入 task_service 队列，info 透传到 _run_task_inner 更新 progress。
    #fix-type-param：type 由 create_task 闭包传入（前端页签决定），默认 video 兜底老调用。
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
        _run_task_inner(task_id, items, category_id, client, manual_wait_ms, info, type=type)
    except _TaskCancelled:
        # #130：取消时业务表标 cancelled + 剩余 pending item 一并清理，re-raise 让外层置 info.status
        logger.info("[分享导入] 任务取消 id={}", task_id)
        try:
            d.execute(
                "UPDATE share_import_item SET status='cancelled', message='任务已取消', update_time=? "
                "WHERE task_id=? AND status='pending' AND deleted=0",
                (now_str(), task_id))
            # #P1-4：取消路径刷 success/failed 计数（前端看到的统计是最新真实值）
            # #fix-music-node：先 UPDATE status='cancelled' + counts，再用 WHERE status NOT IN
            # ('cancelled') 保护的 UPDATE 覆写 counts（不会把 cancelled 状态翻回 completed/failed）。
            _update_task_summary(task_id, len(items))
            d.execute(
                "UPDATE share_import_task SET status='cancelled', message='用户取消', update_time=? "
                "WHERE id=? AND status NOT IN ('cancelled')",
                (now_str(), task_id))
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
                # #fix-music-node：hint 模板用 it["id"][:8]（id 列必须 SELECT）
                "SELECT id, share_text, message FROM share_import_item "
                "WHERE task_id=? AND status='failed' AND deleted=0 LIMIT 3",
                (task_id,),
            )
            if failed_items:
                # #fix-music-node：hint 加 item_id 前 8 位避免 share_text 截断撞前缀
                hints = [
                    f"[{it['id'][:8]}] {(it['share_text'] or '')[:24]}: {it['message'] or '未知'}"
                    for it in failed_items if it.get("message")
                ]
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
                    manual_wait_ms: int = 30000, info=None, type: str = "video") -> None:
    """_run_task 的实际处理循环。

    PR4 #57：info 透传，每完成一条 update info.progress。
    #fix-type-param：type 透传到 _process_item。
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
        status, msg, material_id = _process_item(item, category_id, client,
                                                  manual_wait_ms, type=type, info=info)
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


def _run_single_item(task_id: str, item_id: str, info=None, type: str = "video") -> str:
    """后台执行单条重试（手动触发）。

    PR4 #57：接入 task_service.submit("download", ...)——单条重试也走队列，name 用任务名。
    任务 #59 P0 #5：显式 return "success"/"partial"/"failed" 状态机契约对齐。
    #fix-type-param：retry 也需 type 入参。
    #fix-music-node：入口直接 raise_for_cancel（让 _TaskCancelled 自然冒泡到
    task_service.submit 的 except 分支，info.status="cancelled"——而不是返回
    "cancelled" 字符串被 task_service 当非法值吞为 success）。
    """
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts（无 info 时回退 time.time()）
    start_ts = (info.start_ts if info else None) or time.time()
    # #fix-music-node：取消检查直接抛异常（task_service.submit 兜底）
    if info is not None:
        raise_for_cancel(info)
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
    status, msg, material_id = _process_item(
        item, task["category_id"], client, manual_wait_ms=60000, type=type, info=info)
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


def retry_failed(task_id: str, type: str | None = None) -> int:
    """重试任务中所有失败的项，返回重试项数。

    仅允许 completed/failed 状态的任务发起重试；running/pending 任务不处理。
    #fix-type-param：type 默认从 task 表读（前端可显式传覆盖；老任务 type='video'）。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("completed", "failed"):
        raise ValueError("任务仍在执行中，无法重试")
    # 入参 type 优先；None 时从 task 表读（v43+ 存了 type，老任务 DEFAULT 'video'）
    if type is None:
        type = task.get("type") or "video"
    if type not in ("video", "music"):
        raise ValueError("type 必须为 video 或 music")

    failed_items = d.query_all(
        "SELECT * FROM share_import_item WHERE task_id=? AND status='failed' AND deleted=0",
        (task_id,))
    if not failed_items:
        return 0

    # #fix-music-node：UPDATE 加 WHERE status IN 防止旧 worker 终态被覆盖
    d.execute(
        "UPDATE share_import_task SET status=?, success_count=0, failed_count=0, message=?, update_time=? "
        "WHERE id=? AND status IN ('completed', 'failed')",
        ("running", "重试中…", now_str(), task_id))
    # PR4 #57：走 task_service.submit("download", ...)，name 取分类名（同 create_task）
    # #fix-music-node：type_label 用 type 入参，不用 _detect_kind
    type_label = f"素材下载-{'音乐' if type == 'music' else '视频'}-重试"
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (task["category_id"],)) \
        if task["category_id"] != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    # #fix-type-param：闭包传 type 给 worker
    task_service.submit(
        "download", name, lambda info: _run_task(task_id, info, type=type),
        type_label=type_label,
    )
    logger.info("[分享导入] 发起重试 id={} count={}", task_id, len(failed_items))
    return len(failed_items)


def retry_item(task_id: str, item_id: str, type: str | None = None) -> bool:
    """手动重试单条失败项。

    将目标项标记为 pending 并重试次数 +1，后台异步执行。
    #fix-type-param：type 默认从 task 表读（前端可显式传覆盖）。
    #fix-music-node：校验 task.status 仅在 completed/failed 时允许重试
    （避免用户连续点击导致两个 worker 串行入队、计数残留）。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM share_import_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("completed", "failed"):
        raise ValueError("任务仍在执行中，无法重试")
    # 入参 type 优先；None 时从 task 表读
    if type is None:
        type = task.get("type") or "video"
    if type not in ("video", "music"):
        raise ValueError("type 必须为 video 或 music")
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
    # #fix-music-node：UPDATE 加 WHERE status IN 防止旧 worker 终态被覆盖
    d.execute(
        "UPDATE share_import_task SET status=?, message=?, update_time=? "
        "WHERE id=? AND status IN ('completed', 'failed')",
        ("running", "重试中…", now_str(), task_id))
    # PR4 #57：单条重试也走 task_service.submit("download", ...)
    # #fix-music-node：type_label 用 type 入参，不用 _detect_kind
    type_label = f"素材下载-{'音乐' if type == 'music' else '视频'}-单条重试"
    cat_row = d.query_one("SELECT name FROM material_category WHERE id=?", (task["category_id"],)) \
        if task["category_id"] != ms.UNCATEGORIZED_ID else None
    cat_name = cat_row["name"] if cat_row else "未分类"
    name = f"导入到 {cat_name}"
    # #fix-type-param：闭包传 type 给 worker
    task_service.submit(
        "download", name, lambda info: _run_single_item(task_id, item_id, info, type=type),
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
