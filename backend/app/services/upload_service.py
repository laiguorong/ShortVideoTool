# -*- coding: utf-8 -*-
"""本地文件/文件夹上传任务服务：异步执行、进度落库、失败可重试。

支持：
- 单文件/多文件/文件夹（递归收集）
- 视频文件自动抽帧生成封面
- 上传任务持久化，可查看进度与重试失败项

#119：原 _EXECUTOR 线程池已删除，改用 task_service.submit("upload", ...) 接入
任务队列——状态栏可见、cancel API 可用、并发受 task_service 池控制（max_workers=2）。
"""

import time
from pathlib import Path
from typing import Optional

from loguru import logger

from app.db import get_db
from app.db.utils import new_id, now_str
from app.services.material_service import _upload_single_file
from app.services.material_service import UNCATEGORIZED_ID
from app.services.task_service import task_service, TaskInfo, raise_for_cancel, _fmt_hms, _TaskCancelled

# 视频扩展名白名单（与 material_service 保持一致）
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".m4v"}


def create_task(category_id: str, paths: list[str]) -> tuple[str, str]:
    """创建上传任务，递归展开文件夹，立即返回任务 ID。

    #119：调 task_service.submit("upload", ...) 取代 _EXECUTOR.submit。
    #123：返回 (upload_task_id, bg_task_id)——
    #   upload_task_id 是 upload_task 表主键（前端展示/查询/删除）；
    #   bg_task_id 是 task_service 队列 UUID（前端 cancel 调用 /tasks/{id}/cancel 用）。
    name 用 task_id[:8] + 文件数（upload_task 表无 task_name 字段）。

    #127：upload_item 批量插入事务包裹——1000 文件场景从 1000 次 auto-commit
    # 降到 1 次事务提交，提速约 10-50 倍。
    """
    d = get_db()
    # 允许 UNCATEGORIZED_ID 作为入库目标（虚拟分类，按文件扩展名推断类型）
    if category_id != UNCATEGORIZED_ID:
        cat = d.query_one("SELECT * FROM material_category WHERE id=? AND deleted=0", (category_id,))
        if not cat:
            raise ValueError("分类不存在")

    files = _collect_files(paths)
    task_id = new_id()
    now = now_str()
    d.insert("upload_task", {
        "id": task_id,
        "category_id": category_id,
        "status": "pending",
        "total": len(files),
        "success_count": 0,
        "failed_count": 0,
        "message": "等待执行",
        "create_time": now,
        "update_time": now,
    })
    # #127：批量插入 upload_item（事务包裹）
    item_rows = []
    for fp in files:
        p = Path(fp)
        item_rows.append({
            "id": new_id(),
            "task_id": task_id,
            "file_path": str(fp),
            "file_name": p.name,
            "status": "pending",
            "message": "",
            "retry_count": 0,
            "create_time": now,
            "update_time": now,
        })
    if item_rows:
        with d.transaction():
            for row in item_rows:
                d.insert("upload_item", row)
    name = f"上传 #{task_id[:8]}（{len(files)} 个文件）"
    # #119：task_service 状态机 waiting → running → success/partial/failed
    # 真实工作状态落 upload_task.status（pending/running/completed/failed/partial），
    # task_service 只承担队列调度 + 取消信号 + 状态栏可见性。
    bg_id = task_service.submit(
        "upload",
        name,
        lambda info: _run_task(task_id, info),
    )
    logger.info("[上传任务] 创建 id={} bg_task={} category={} total={}",
                task_id, bg_id, category_id, len(files))
    return task_id, bg_id


def _collect_files(paths: list[str]) -> list[str]:
    """递归收集文件路径（去重、按路径排序）。"""
    seen: set[str] = set()
    out: list[str] = []
    stack = list(paths)
    while stack:
        raw = stack.pop()
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_dir():
            # 递归加入子项（排序保证稳定）
            for child in sorted(p.iterdir(), key=lambda x: x.name, reverse=True):
                stack.append(str(child))
        elif p.is_file():
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                out.append(str(p))
    out.sort()
    return out


def _process_item(item: dict, category_id: str) -> tuple[str, str, Optional[str]]:
    """处理单个文件上传：返回 (状态, 消息, material_id)。"""
    try:
        r = _upload_single_file(item["file_path"], category_id)
        if r and r["ok"]:
            return "success", "上传成功", r.get("material_id")
        return "failed", (r["message"] if r else "未知错误"), None
    except Exception as e:  # noqa: BLE001
        logger.exception("[上传任务] 异常 file={}", item["file_path"])
        return "failed", str(e), None


def _update_task_summary(task_id: str, total: int, success: int, failed: int) -> None:
    """更新 upload_task 主记录。

    #122：扩 status 加 "partial"——success>0 且 failed>0 时标 partial，前端 StatusBadge 已支持。
    #128：接受 success/failed 参数（worker 内已统计），避免双源不一致。
    """
    if failed == 0 and success == 0:
        final_status = "pending"
    elif failed == 0:
        final_status = "completed"
    elif success == 0:
        final_status = "failed"
    else:
        final_status = "partial"
    msg = f"成功 {success} / 失败 {failed} / 总计 {total}"
    d = get_db()
    d.execute(
        "UPDATE upload_task SET status=?, success_count=?, failed_count=?, message=?, update_time=? WHERE id=?",
        (final_status, success, failed, msg, now_str(), task_id))
    logger.info("[上传任务] 任务汇总更新 id={} status={} {}", task_id, final_status, msg)


def _update_task_summary_from_db(task_id: str) -> None:
    """#P1-5：从 DB 实时 COUNT 重新计算 success/failed，写入 upload_task。

    单条重试场景下硬传 0/1 会让多次重试累加错乱，改为读真实值。
    """
    d = get_db()
    rows = d.query_one(
        "SELECT "
        "  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS s, "
        "  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f, "
        "  SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS c, "
        "  COUNT(*) AS total "
        "FROM upload_item WHERE task_id=? AND deleted=0",
        (task_id,))
    if not rows or not rows.get("total"):
        return
    s = int(rows["s"] or 0)
    f = int(rows["f"] or 0)
    c = int(rows["c"] or 0)
    total = int(rows["total"])
    if c > 0:
        new_status = "cancelled"
    elif f == 0:
        new_status = "success"
    elif s == 0:
        new_status = "failed"
    else:
        new_status = "partial"
    msg = f"成功 {s} / 失败 {f} / 总计 {total}"
    d.execute(
        "UPDATE upload_task SET status=?, success_count=?, failed_count=?, message=?, update_time=? WHERE id=?",
        (new_status, s, f, msg, now_str(), task_id))


def _run_task(task_id: str, info: Optional[TaskInfo] = None) -> str:
    """task_service worker：执行上传任务。

    #119：原线程池版本改为接收 info，支持取消与进度上报。
    返回:
        "success" / "partial" / "failed"（task_service 状态机契约）
    """
    d = get_db()
    d.execute("UPDATE upload_task SET status=?, update_time=? WHERE id=?",
              ("running", now_str(), task_id))

    task = d.query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))
    category_id = task["category_id"] if task else ""
    items = d.query_all("SELECT * FROM upload_item WHERE task_id=? AND deleted=0 ORDER BY create_time",
                        (task_id,))

    # #120：info.start_ts 是 Optional[float]——None 时回退 time.time()，避免 None - time.time() TypeError
    start_ts = (info.start_ts if info else None) or time.time()

    success_count = 0
    failed_count = 0
    # #130：包 try/except _TaskCancelled——取消时把 upload_task 标 cancelled + 释放未处理的 item
    try:
        for item in items:
            if info:
                raise_for_cancel(info)
            if item["status"] == "success":
                success_count += 1
                continue
            status, msg, material_id = _process_item(item, category_id)
            # 首次执行不增加重试次数，仅手动重试时累加
            inc = 0 if item["status"] == "pending" else 1
            # #126：UPDATE 失败容错（DB 锁等）——静默置该 item 为 failed，避免单条异常中断整 worker
            try:
                d.execute(
                    "UPDATE upload_item SET status=?, message=?, material_id=?, retry_count=retry_count+?, update_time=? WHERE id=?",
                    (status, msg, material_id, inc, now_str(), item["id"]))
            except Exception as e:  # noqa: BLE001
                logger.error("[上传任务] 写 item 失败 item={} 异常={}", item["id"], e)
                status = "failed"
                msg = f"DB 写失败：{e}"
            logger.info("[上传任务] item={} status={} file={}", item["id"], status, item["file_path"])
            if status == "success":
                success_count += 1
            else:
                failed_count += 1
            # #119：每文件完成刷一次 progress（与 video 重活同模式）
            if info:
                elapsed = time.time() - start_ts
                # #126：progress 写失败容错（store 闭包异常不应中断 worker）
                try:
                    info.progress = (
                        f"已上传 {success_count + failed_count}/{len(items)}，"
                        f"用时 {_fmt_hms(elapsed)}"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("[上传任务] 刷 progress 失败：{}", e)
        # 正常结束：#128 复用本函数统计的计数
        _update_task_summary(task_id, len(items), success_count, failed_count)
        # 终止状态下写 partial message
        if info and failed_count > 0 and success_count > 0:
            try:
                info.message = f"失败 {failed_count} 条"
            except Exception:  # noqa: BLE001
                pass
        elif info and failed_count > 0:
            try:
                info.message = f"全部失败 {failed_count} 条"
            except Exception:  # noqa: BLE001
                pass
        return "success" if failed_count == 0 else ("partial" if success_count > 0 else "failed")
    except _TaskCancelled:
        # #130：worker 抛 _TaskCancelled 时——把已处理 item 数落库，剩余 item 标 cancelled，
        # 然后 re-raise 让 task_service._run 统一置 info.status=cancelled
        logger.info("[上传任务] 任务取消 id={} 已处理 {}/{}", task_id, success_count + failed_count, len(items))
        try:
            d.execute(
                "UPDATE upload_task SET status=?, message=?, update_time=? WHERE id=?",
                ("cancelled", f"用户取消（已处理 {success_count + failed_count}/{len(items)}）", now_str(), task_id))
            # 剩余 pending item 标 cancelled（已处理的保持原状态）
            d.execute(
                "UPDATE upload_item SET status='cancelled', message='任务已取消', update_time=? "
                "WHERE task_id=? AND status='pending' AND deleted=0",
                (now_str(), task_id))
            # #P1-4：取消路径刷 success/failed 计数 + 写表汇总
            _update_task_summary(task_id, len(items))
        except Exception as e:  # noqa: BLE001
            logger.error("[上传任务] 取消清理写 DB 失败：{}", e)
        raise


def _run_single_item(task_id: str, item_id: str, info: Optional[TaskInfo] = None) -> str:
    """task_service worker：单条上传重试（手动触发）。

    #121：加 cancel 响应——retry_item 投任务后用户取消，worker 立即停止。
    """
    if info:
        # 入口检查一次：worker 启动时已 cancel 时直接 return
        try:
            raise_for_cancel(info)
        except Exception:
            return "cancelled"
    d = get_db()
    task = d.query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        return "failed"
    item = d.query_one(
        "SELECT * FROM upload_item WHERE id=? AND task_id=? AND deleted=0",
        (item_id, task_id))
    if not item or item["status"] != "pending":
        return "failed"
    status, msg, material_id = _process_item(item, task["category_id"])
    # 重试次数已在标记重试时累加，此处不再增加
    try:
        d.execute(
            "UPDATE upload_item SET status=?, message=?, material_id=?, update_time=? WHERE id=?",
            (status, msg, material_id, now_str(), item_id))
    except Exception as e:  # noqa: BLE001
        logger.error("[上传任务] 单条重试 写失败 item={} 异常={}", item_id, e)
        status = "failed"
        msg = f"DB 写失败：{e}"
    logger.info("[上传任务] 单条重试 item={} status={} file={}", item_id, status, item["file_path"])
    # #P1-5：从 DB COUNT 真实统计，不再 hardcode 0/1 避免多次重试累加错乱
    _update_task_summary_from_db(task_id)
    if info:
        try:
            info.message = "重试完成" if status == "success" else f"重试失败：{msg}"
        except Exception:  # noqa: BLE001
            pass
    return "success" if status == "success" else "failed"


def get_task(task_id: str) -> Optional[dict]:
    """查询上传任务主记录。"""
    return get_db().query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))


def list_tasks(page: int = 1, page_size: int = 20) -> dict:
    """分页查询上传任务列表（含目标分类名）。"""
    return get_db().query_page(
        """SELECT t.*, c.name AS category_name
           FROM upload_task t
           LEFT JOIN material_category c ON t.category_id=c.id
           WHERE t.deleted=0 ORDER BY t.create_time DESC""",
        (), page, page_size)


def list_items(task_id: str) -> list[dict]:
    """查询上传任务明细列表。"""
    return get_db().query_all("SELECT * FROM upload_item WHERE task_id=? AND deleted=0 ORDER BY create_time",
                              (task_id,))


def retry_failed(task_id: str) -> int:
    """重试任务中失败的项，返回重试项数。

    #119：不再走 _EXECUTOR，改投 task_service 队列（type=upload）。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("completed", "failed"):
        raise ValueError("任务仍在执行中，无法重试")

    failed_items = d.query_all(
        "SELECT * FROM upload_item WHERE task_id=? AND status='failed' AND deleted=0",
        (task_id,))
    if not failed_items:
        return 0

    # 把 failed item 重置为 pending（worker _run_task 会跳过 status=success 的，处理 remaining pending）
    placeholders = ",".join("?" * len(failed_items))
    ids = [it["id"] for it in failed_items]
    d.execute(
        f"UPDATE upload_item SET status='pending', message='重试中…', update_time=? WHERE id IN ({placeholders})",
        tuple([now_str()] + ids),
    )
    # 不清零 success_count/failed_count：worker 收尾 _update_task_summary 会按 DB 真实统计重写
    # （清零反而让用户在前端重试窗口看到 0/0 误以为"已重置成功"）
    d.execute("UPDATE upload_task SET status=?, message=?, update_time=? WHERE id=?",
              ("running", "重试中…", now_str(), task_id))
    name = f"上传重试 #{task_id[:8]}（{len(failed_items)} 个文件）"
    task_service.submit(
        "upload",
        name,
        lambda info: _run_task(task_id, info),
    )
    logger.info("[上传任务] 发起重试 id={} count={}", task_id, len(failed_items))
    return len(failed_items)


def retry_item(task_id: str, item_id: str) -> bool:
    """手动重试单条失败上传项。

    #119：单条重试也走 task_service（type=upload）；cancel 信号有效。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    item = d.query_one(
        "SELECT * FROM upload_item WHERE id=? AND task_id=? AND deleted=0",
        (item_id, task_id))
    if not item:
        raise ValueError("明细项不存在")
    if item["status"] != "failed":
        raise ValueError("仅失败项可重试")

    d.execute(
        "UPDATE upload_item SET status=?, message=?, retry_count=retry_count+1, update_time=? WHERE id=?",
        ("pending", "等待重试", now_str(), item_id))
    d.execute("UPDATE upload_task SET status=?, message=?, update_time=? WHERE id=?",
              ("running", "重试中…", now_str(), task_id))
    name = f"上传单条重试 #{task_id[:8]}"
    task_service.submit(
        "upload",
        name,
        lambda info, iid=item_id: _run_single_item(task_id, iid, info),
    )
    logger.info("[上传任务] 单条重试 task={} item={}", task_id, item_id)
    return True


def delete_task(task_id: str) -> None:
    """删除上传任务及其明细（软删除）。执行中任务不可删。"""
    d = get_db()
    task = d.query_one("SELECT * FROM upload_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] in ("running", "pending"):
        raise ValueError("任务执行中，无法删除")
    now = now_str()
    d.execute("UPDATE upload_task SET deleted=1, update_time=? WHERE id=?", (now, task_id))
    d.execute("UPDATE upload_item SET deleted=1, update_time=? WHERE task_id=?", (now, task_id))
    logger.info("[上传任务] 删除任务 id={}", task_id)