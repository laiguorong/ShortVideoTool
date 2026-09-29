# -*- coding: utf-8 -*-
"""发布中心服务（F-05）：发布任务排期、明细状态机、执行发布、发布记录留痕（F-06）。

状态机（F-05-R6 / 附录 10.1）：
明细 waiting → publishing → success / failed；waiting ⇄ suspended（账号失效挂起/恢复）；waiting → cancelled。
"""

import json
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from app.core import notifier
from app.core.douyin import get_douyin_client, LoginInvalidError, RiskControlError
from app.db import get_db
from app.db.utils import now_str
from app.services import account_service, creation_service
from app.services.publish_schedule import (
    IntroSpec, ProjectSpec, ScheduleConfig, ScheduleOverflowError,
    VideoDirProjectSpec, VideoDirScheduleConfig, VideoDirScheduleItem,
    build_schedule, build_video_dir_schedule,
)
from app.services.task_service import _fmt_hms, raise_for_cancel, task_service, interruptible_sleep, _TaskCancelled
from app.services.video_dir_service import scan_video_dir as _scan_video_dir

# 发布失败重试上限（F-05-R4）
MAX_RETRY = 3
# 任务重试的最小计划时间间隔：plan_time 距 now 不足该阈值 → 直接关闭，不可重试
RETRY_MIN_GAP_HOURS = 2


# ---------- 任务创建（F-05.1 / F-05-R1） ----------

# 默认字段值（#413 发布管理重设计）
_DEFAULT_DECLARATION = "ai_generated"   # ai_generated / none
_DEFAULT_ALLOW_DOWNLOAD = 0
# #calc_mode：迁移期保留旧字段 fallback 默认值（仅作 schema 默认 / 旧列兼容，不再被业务硬编码写入）
_DEFAULT_SAME_INTERVAL = 60
_DEFAULT_DIFF_INTERVAL = 10
# #calc_mode：新字段默认值
_DEFAULT_SCHEDULE_MODE = "balanced"
_DEFAULT_VIDEO_DIR_SCHEDULE_MODE = "fixed"  # 视频目录模式默认 fixed（用户场景）
_DEFAULT_FIXED_INTERVAL = 10
_DEFAULT_BALANCED_STEP = 60


def _parse_flexible(s: str) -> datetime:
    """#420：兼容无秒（yyyy-MM-dd HH）与带秒两种格式（旧 [旧] create_task 接口透传历史草稿）。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式无法解析：{s}（应为 yyyy-MM-dd HH:mm）")


def _default_start_end() -> tuple[str, str]:
    """默认起始 = 明天 07:00，结束 = 明天 22:00。"""
    tomorrow = datetime.now() + timedelta(days=1)
    start = tomorrow.replace(hour=7, minute=0, second=0, microsecond=0)
    end = tomorrow.replace(hour=22, minute=0, second=0, microsecond=0)
    return (start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"))


def _resolve_intros(project_id: str) -> list[dict]:
    """取项目所有启用的简介 + 话题，顺序按 create_time。"""
    from app.services import publish_intro_service
    rows = publish_intro_service.list_intros(project_id)
    return [r for r in rows if r.get("enabled", 1)]


def _resolve_project_shops(project_id: str) -> list[str]:
    """取项目当前可发布的门店 ID 列表（绑定的 ∩ is_added=1 ∩ is_cps=1，按 sort_order）。

    #444：业务硬约束——未加库 / 无佣金的店不能进入发布流程。
    #445：preview/confirm 用此集合判断 draft 勾的店是否仍可发布。
    """
    from app.services import publish_intro_service
    rows = publish_intro_service.list_project_shops(project_id)
    return [r["shop_id"] for r in rows]


def _resolve_raw_project_shop_ids(project_id: str) -> set[str]:
    """取项目已绑定门店 ID 全集（不过滤 is_added/is_cps；用于 extra_in_draft 检测）。

    #445：用于检测 draft 勾的店是否曾绑定项目但已被彻底移除（区别于 stale_in_draft 的"业务失效"）。
    """
    d = get_db()
    return {r["shop_id"] for r in d.query_all(
        "SELECT shop_id FROM project_shop WHERE project_id=?",
        (project_id,))}


def _resolve_schedule_fields(src: dict) -> tuple[str, int, int, int | None]:
    """#calc_mode：三字段 + same_project_interval_min 统一解析（三处共用，保证 fallback 链一致）。

    行为：
    - schedule_mode：None → 'fixed'（is None 检查，避免 0/false 陷阱）
    - fixed_interval_min：None → _DEFAULT_FIXED_INTERVAL
    - balanced_step_min：None → fallback 到 same_project_interval_min（仍 None）→ _DEFAULT_BALANCED_STEP
    - same_project_interval_min：None → None（保留 None，由调用方按需走 DEFAULT）

    Returns:
        (schedule_mode, fixed_interval_min, balanced_step_min, same_project_interval_min)
    """
    schedule_mode = src.get("schedule_mode") if src.get("schedule_mode") is not None else _DEFAULT_VIDEO_DIR_SCHEDULE_MODE
    fixed_interval_min = (
        int(src["fixed_interval_min"]) if src.get("fixed_interval_min") is not None
        else _DEFAULT_FIXED_INTERVAL
    )
    balanced_step_min = (
        int(src["balanced_step_min"]) if src.get("balanced_step_min") is not None
        else (int(src["same_project_interval_min"]) if src.get("same_project_interval_min") is not None
              else _DEFAULT_BALANCED_STEP)
    )
    same_proj = src.get("same_project_interval_min")
    same_project_interval_min = int(same_proj) if same_proj is not None else None
    return schedule_mode, fixed_interval_min, balanced_step_min, same_project_interval_min


def save_draft(task_name: str,
               projects_payload: list[dict],
               account_ids: list[str],
               daily_limit_mode: str,
               daily_limit_global: int,
               daily_limit_per_account: list[tuple[str, int]],
               start_time: str | None = None,
               end_time: str | None = None,
               # #calc_mode 新字段（默认 balanced 60min，向后兼容旧调用）
               schedule_mode: str = _DEFAULT_SCHEDULE_MODE,
               fixed_interval_min: int = _DEFAULT_FIXED_INTERVAL,
               balanced_step_min: int = _DEFAULT_BALANCED_STEP,
               # 旧字段保留（向后兼容旧调用，balanced_step_min 优先）
               same_project_interval_min: int = _DEFAULT_SAME_INTERVAL,
               diff_project_interval_min: int = _DEFAULT_DIFF_INTERVAL,
               declaration: str = _DEFAULT_DECLARATION,
               allow_download: bool = False,
               task_id: str | None = None,
               # #418：视频目录模式字段
               project_source: str = "project",
               video_dirs: list[dict] | None = None,
               manual_shops: dict[str, list[dict]] | None = None,
               # 视频简介-按项目一一对应：{dir_id: [str, ...]}
               manual_intros: dict[str, list[str]] | None = None) -> dict:
    """保存草稿（#413 拆分版）：写 publish_task(status=draft)，不展开明细。

    R12 修：传 task_id 时改为 UPDATE（用于 wizard 回退后重保存），
    否则 INSERT 新 draft。
    参数:
        projects_payload: [{"project_id": str, "shop_ids": [..], "intro_ids": [..]}, ...]
            shop_ids / intro_ids 是用户在前端勾选的顺序（可选；为空时回查项目绑定）
        #calc_mode：schedule_mode / fixed_interval_min / balanced_step_min 三个新字段；
            旧调用只传 same/diff_project_interval_min 也兼容——会落 balanced_step_min。
    返回:
        {"task_id": str, "draft": True}
    """
    if not account_ids:
        raise ValueError("账号列表不能为空")
    # #418：视频目录模式 project_source='video_dir' 时不强制 projects_payload 非空
    if project_source != "video_dir" and not projects_payload:
        raise ValueError("项目列表不能为空")
    if project_source == "video_dir" and not video_dirs:
        raise ValueError("视频目录列表不能为空")

    d = get_db()
    default_start, default_end = _default_start_end()
    start_time = start_time or default_start
    end_time = end_time or default_end

    # 账号校验
    for aid in account_ids:
        acc = d.query_one("SELECT status FROM account WHERE id=? AND deleted=0", (aid,))
        if not acc:
            raise ValueError("账号不存在")
        if acc["status"] != "normal":
            raise ValueError("账号非正常状态，请先处理登录态")

    # #calc_mode 兼容旧调用：如果 caller 只传了 same_project_interval_min，未传 balanced_step_min（仍为 None/默认 60），
    # 把 same_project_interval_min 当作 balanced_step_min 写库（旧行为等价）。
    # 注意：显式传 balanced_step_min=60 的合法输入不应被旧字段覆盖——下方判断用 None 占位 sentinel 不够，
    # 改用「balanced_step_min 未显式传入」的标志位。
    # 由于函数签名是 `balanced_step_min: int = _DEFAULT_BALANCED_STEP`，无法区分"传了 60"与"用默认 60"；
    # 这里采取保守策略：仅当 same_project_interval_min 非默认 60 时才覆盖 balanced_step_min，
    # 避免 caller 主动传 60 时被 silent 覆盖。
    if same_project_interval_min != _DEFAULT_SAME_INTERVAL:
        # 旧调用方显式传了 same_project_interval_min（旧参数路径），把它写到 balanced_step_min
        balanced_step_min = same_project_interval_min
    # interval_minutes 列保留（兼容旧 UI 读出）
    interval_minutes_value = fixed_interval_min if schedule_mode == "fixed" else balanced_step_min

    if task_id:
        # UPDATE 路径：仅允许 draft 状态覆盖
        cur = d.query_one("SELECT status FROM publish_task WHERE id=? AND deleted=0", (task_id,))
        if not cur:
            raise ValueError(f"草稿 {task_id} 不存在")
        if cur["status"] != "draft":
            raise ValueError(f"任务状态 {cur['status']} 不可覆盖（仅 draft 可重新保存）")
        d.update_by_id("publish_task", task_id, {
            "task_name": task_name,
            "project_ids_json": json.dumps([p["project_id"] for p in projects_payload]),
            "account_ids_json": json.dumps(account_ids),
            "shop_ids_json": json.dumps([]),
            "per_account_count": daily_limit_global if daily_limit_mode == "global" else 0,
            "start_time": start_time,
            "interval_minutes": interval_minutes_value,
            "shop_assign_mode": "round",
            "status": "draft",
            "end_time": end_time,
            "daily_limit_mode": daily_limit_mode,
            "daily_limit_global": daily_limit_global,
            "daily_limit_per_account_json": json.dumps(
                [{"account_id": aid, "limit": lim} for aid, lim in daily_limit_per_account],
                ensure_ascii=False),
            # #calc_mode 新字段
            "schedule_mode": schedule_mode,
            "fixed_interval_min": fixed_interval_min,
            "balanced_step_min": balanced_step_min,
            # 旧字段保留
            "same_project_interval_min": same_project_interval_min,
            "diff_project_interval_min": diff_project_interval_min,
            "declaration": declaration,
            "allow_download": 1 if allow_download else 0,
            "projects_payload_json": json.dumps(projects_payload, ensure_ascii=False),
            # #418：视频目录模式字段
            "project_source": project_source,
            "video_dirs_json": json.dumps(video_dirs or [], ensure_ascii=False),
            "manual_shops_json": json.dumps(manual_shops or {}, ensure_ascii=False),
            "manual_intros_json": json.dumps(manual_intros or {}, ensure_ascii=False),
        })
        return {"task_id": task_id, "draft": True}

    # INSERT 路径（首次落草稿）
    # interval_minutes 字段 deprecated（#413 审查 #23）：保留以兼容旧记录；
    # 新逻辑读 schedule_mode / fixed_interval_min / balanced_step_min。
    new_task_id = d.insert("publish_task", {
        "task_name": task_name,
        "project_ids_json": json.dumps([p["project_id"] for p in projects_payload]),
        "account_ids_json": json.dumps(account_ids),
        "shop_ids_json": json.dumps([]),
        "per_account_count": daily_limit_global if daily_limit_mode == "global" else 0,
        "start_time": start_time,
        "interval_minutes": interval_minutes_value,
        "shop_assign_mode": "round",
        "status": "draft",
        "end_time": end_time,
        "daily_limit_mode": daily_limit_mode,
        "daily_limit_global": daily_limit_global,
        "daily_limit_per_account_json": json.dumps(
            [{"account_id": aid, "limit": lim} for aid, lim in daily_limit_per_account],
            ensure_ascii=False),
        # #calc_mode 新字段
        "schedule_mode": schedule_mode,
        "fixed_interval_min": fixed_interval_min,
        "balanced_step_min": balanced_step_min,
        # 旧字段保留
        "same_project_interval_min": same_project_interval_min,
        "diff_project_interval_min": diff_project_interval_min,
        "declaration": declaration,
        "allow_download": 1 if allow_download else 0,
        # #418：视频目录模式字段
        "project_source": project_source,
        "video_dirs_json": json.dumps(video_dirs or [], ensure_ascii=False),
        "manual_shops_json": json.dumps(manual_shops or {}, ensure_ascii=False),
        "manual_intros_json": json.dumps(manual_intros or {}, ensure_ascii=False),
    })
    d.update_by_id("publish_task", new_task_id, {
        "projects_payload_json": json.dumps(projects_payload, ensure_ascii=False),
    })
    return {"task_id": new_task_id, "draft": True}


def preview_task(task_id: str) -> dict:
    """计算排期预览（#413）：不入库、不占成品。"""
    d = get_db()
    task = d.query_one("SELECT * FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("draft", "running", "paused", "finished"):
        raise ValueError(f"任务状态 {task['status']} 不支持预览")

    projects_payload = json.loads(task.get("projects_payload_json") or "[]")
    if not projects_payload:
        raise ValueError("草稿缺少项目配置")

    # 构造 ScheduleConfig（#413 审查 #2：回查结果固化到 projects_payload_json，
    # 避免下次 preview 拿到最新绑定而与用户预期不一致）
    projects: list[ProjectSpec] = []
    payload_dirty = False
    # #439 + #445：B 方案——提交前校验 draft 与 DB 一致性，收集 stale_in_draft / extra_in_draft
    # 给前端 wizard 拦截使用，避免 stale 草稿带病进入排期
    validation_warnings: list[dict] = []
    for p in projects_payload:
        project_id = p["project_id"]
        # #438 修：之前用 `p.get("shop_ids") or _resolve_project_shops(...)`，
        # `or` 把空 list 视为 falsy → fallback 到 DB 全部绑定（含 wizard step3 未显示/未选的店）。
        # 改为：明确 None 才 fallback（兼容旧草稿缺字段）；空 list = 用户意图（虽 canAdvance 阻止）。
        shop_ids_payload = p.get("shop_ids")
        if shop_ids_payload is None:
            shop_ids = _resolve_project_shops(project_id)
            p["shop_ids"] = shop_ids
            payload_dirty = True
            logger.warning(
                "[preview] 项目 {} draft.shop_ids 缺失，fallback 到 DB 全部 {} 家: {}",
                project_id, len(shop_ids), shop_ids)
        else:
            shop_ids = shop_ids_payload
            # #445：可发布集合 = project_shop 绑定 ∩ is_added=1 ∩ is_cps=1
            publishable_shops = set(_resolve_project_shops(project_id))
            draft_set = set(shop_ids)
            # stale_in_draft：draft 勾的但当前不可用
            stale_in_draft = [s for s in shop_ids if s not in publishable_shops]
            # extra_in_draft：draft 勾的店曾绑定项目但已被彻底移除
            raw_binding = _resolve_raw_project_shop_ids(project_id)
            extra_in_draft = [s for s in shop_ids if s not in raw_binding]
            db_shop_ids = _resolve_project_shops(project_id)
            logger.info(
                "[preview] 项目 {} draft.shop_ids={} ({}) / publishable={} ({}) / raw_binding={}",
                project_id, shop_ids, len(shop_ids),
                db_shop_ids, len(db_shop_ids), len(raw_binding))
            if stale_in_draft:
                logger.warning(
                    "[preview] 项目 {} draft 中 {} 家店当前不可用（被取消加库/无佣金/解除绑定）: {}",
                    project_id, len(stale_in_draft), stale_in_draft)
            if extra_in_draft:
                logger.warning(
                    "[preview] 项目 {} draft 中 {} 家店已彻底解除绑定: {}",
                    project_id, len(extra_in_draft), extra_in_draft)
            # #439：非空时收集到 warnings 返回给前端
            if stale_in_draft or extra_in_draft:
                validation_warnings.append({
                    "project_id": project_id,
                    "stale_in_draft": stale_in_draft,
                    "extra_in_draft": extra_in_draft,
                })
        # 简介字段同样改为 None 才 fallback
        intro_ids_payload = p.get("intro_ids")
        if intro_ids_payload is None:
            intro_ids = [r["id"] for r in _resolve_intros(project_id)]
            p["intro_ids"] = intro_ids
            payload_dirty = True
        else:
            intro_ids = intro_ids_payload
        if not shop_ids:
            raise ValueError(f"项目 {project_id} 门店为空")
        # #456：视频简介允许为空（前端不勾选/不输入 → intro_ids=[] → 表示不添加视频简介）
        # 不再 raise；ProjectSpec.intros=() 视为有效配置

        # 简介表查 topics
        all_intros = {r["id"]: r for r in _resolve_intros(project_id)}
        intros: list[IntroSpec] = []
        for iid in intro_ids:
            row = all_intros.get(iid)
            if not row:
                continue
            topics = row.get("topics") or []
            intros.append(IntroSpec(id=row["id"], content=row["content"], topics=tuple(topics)))

        # 门店名/类目查 shop（用于快照，但 preview 不入快照——仅 items 返回）
        projects.append(ProjectSpec(
            id=project_id, title="",
            shop_ids=tuple(shop_ids), intros=tuple(intros)))

    # 每天上限解析（#413 审查 #9：避免 None/0 误判——显式 None 检查）
    if task["daily_limit_mode"] == "global":
        per_acc = ()
        gl = task["daily_limit_global"]
        global_lim = 0 if gl is None else int(gl)
    else:
        raw = json.loads(task.get("daily_limit_per_account_json") or "[]")
        per_acc = tuple((r["account_id"], int(r["limit"])) for r in raw)
        global_lim = 0

    # C6 修：dirty 写回改事务内，原子保证
    if payload_dirty:
        with d.transaction():
            d.update_by_id("publish_task", task_id, {
                "projects_payload_json": json.dumps(projects_payload, ensure_ascii=False),
            })

    cfg = ScheduleConfig(
        account_ids=tuple(json.loads(task["account_ids_json"])),
        projects=tuple(projects),
        daily_limit_mode=task["daily_limit_mode"],
        daily_limit_global=global_lim,
        daily_limit_per_account=per_acc,
        start_time=task["start_time"],
        end_time=task["end_time"],
        # #calc_mode：优先新字段，旧字段 fallback（用 is None 避免 0 陷阱）
        schedule_mode=task.get("schedule_mode") if task.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE,
        fixed_interval_min=int(task["fixed_interval_min"]) if task.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL,
        balanced_step_min=int(task["balanced_step_min"]) if task.get("balanced_step_min") is not None else (
            int(task["same_project_interval_min"]) if task.get("same_project_interval_min") is not None else _DEFAULT_BALANCED_STEP
        ),
        same_project_interval_min=task.get("same_project_interval_min"),
        diff_project_interval_min=task.get("diff_project_interval_min"),
    )
    try:
        items, stats = build_schedule(cfg)
    except ScheduleOverflowError as e:
        return {"task_id": task_id, "overflow": True, "message": str(e),
                "items": [], "stats": {},
                "validation": {"warnings": validation_warnings, "has_warnings": bool(validation_warnings)}}

    return {
        "task_id": task_id,
        "overflow": False,
        "items": [
            {
                "account_id": it.account_id,
                "project_id": it.project_id,
                "shop_id": it.shop_id,
                "intro_id": it.intro_id,
                "intro_content": it.intro_content,
                "intro_topics": list(it.intro_topics),
                "plan_time": it.plan_time,
            }
            for it in items
        ],
        "stats": {
            "total": stats.total,
            "by_account": stats.by_account,
            "by_project": stats.by_project,
        },
        # #439：draft 与 DB 一致性校验结果，供前端 wizard 拦截/提示
        "validation": {
            "warnings": validation_warnings,
            "has_warnings": bool(validation_warnings),
        },
    }


def _confirm_task_from_video_dir(task: dict) -> dict:
    """#418：从 publish_task(video_dir) 重建 payload 调 _confirm_video_dir。

    旧 draft（status=draft）处理：先物理删旧 task 行（items + task_id），
    再 INSERT 新 running 任务。返回的 task_id 是新任务的 ID（与旧 draft 不同）。
    """
    import json as _json
    # #calc_mode：统一解析三字段（None 检查 + balanced→same→DEFAULT fallback）
    schedule_mode, fixed_interval_min, balanced_step_min, same_project_interval_min = _resolve_schedule_fields(task)
    payload = {
        "task_name": task["task_name"],
        "account_ids": _json.loads(task["account_ids_json"] or "[]"),
        "daily_limit_mode": task["daily_limit_mode"] or "global",
        "daily_limit_global": task["daily_limit_global"] or 0,
        "daily_limit_per_account": _json.loads(task.get("daily_limit_per_account_json") or "[]"),
        "start_time": task["start_time"],
        "end_time": task["end_time"],
        # #calc_mode：三字段透传用户原始选择；same_project_interval_min 也透传（中段 fallback 需要）
        "schedule_mode": schedule_mode,
        "fixed_interval_min": fixed_interval_min,
        "balanced_step_min": balanced_step_min,
        "same_project_interval_min": same_project_interval_min,
        "declaration": task.get("declaration") or _DEFAULT_DECLARATION,
        "allow_download": bool(task.get("allow_download")),
        "project_source": "video_dir",
        "video_dirs": _json.loads(task.get("video_dirs_json") or "[]"),
        "manual_shops": _json.loads(task.get("manual_shops_json") or "{}"),
        # 视频简介-按项目一一对应：从 DB JSON 还原 {dir_id: [str, ...]}
        "manual_intros": _json.loads(task.get("manual_intros_json") or "{}"),
    }
    d = get_db()
    old_task_id = task["id"]
    result = _confirm_video_dir(payload)
    # 旧 draft 物理删除（之前 _confirm_video_dir 不会清）
    try:
        d.execute("DELETE FROM publish_task_item WHERE task_id=?", (old_task_id,))
        d.execute("DELETE FROM publish_task WHERE id=?", (old_task_id,))
    except Exception as exc:
        logger.warning("[confirm_task-video-dir] 删除旧 draft {} 失败: {}", old_task_id, exc)
    return result


def confirm_task(task_id: str) -> dict:
    """确认草稿 → 落库明细 + 占成品 + draft→running。

    #418：视频目录模式走 _confirm_video_dir 旁路；不走 generated_video 占用。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] != "draft":
        raise ValueError(f"任务状态 {task['status']} 不可确认（仅 draft 可确认）")
    # #418：视频目录模式早返回（不走 preview_task / 水量校验 / 占成品）
    if (task.get("project_source") or "project") == "video_dir":
        return _confirm_task_from_video_dir(task)

    preview = preview_task(task_id)
    if preview.get("overflow"):
        raise ValueError(preview.get("message") or "时间窗不足")

    items = preview["items"]
    # 水量校验
    needed = len(items)
    project_ids_set = {it["project_id"] for it in items}
    marks = ",".join("?" * len(project_ids_set))
    available = d.query_one(
        f"SELECT COUNT(*) AS c FROM generated_video WHERE project_id IN ({marks}) AND status='idle'",
        tuple(project_ids_set))["c"]
    if available < needed:
        # C1 修：补充建议操作（去创作中心切割更多 / 减少每天上限 / 删除其他 draft 占用的成品）
        raise ValueError(
            f"可用水量不足：需 {needed} 条，可用 {available} 条，缺 {needed - available} 条。"
            f"建议：① 减少「发布数量」；② 去「创作中心」生成更多成品；③ 取消/删除其他 draft 释放成品占用"
        )

    # 按 (project_id, create_time) 顺序取用成品
    with d.transaction():
        inserted = 0
        cursor: dict[str, list[dict]] = {}
        for pid in project_ids_set:
            cursor[pid] = d.query_all(
                "SELECT * FROM generated_video WHERE project_id=? AND status='idle' ORDER BY create_time",
                (pid,))
        for it in items:
            pool = cursor.get(it["project_id"]) or []
            if not pool:
                raise ValueError(f"项目 {it['project_id']} 成品不足")
            video = pool.pop(0)
            # 取门店名快照（#413 审查 #10：缺门店→清晰报错，不抛 KeyError）
            shop = d.query_one("SELECT * FROM shop WHERE id=? AND deleted=0", (it["shop_id"],))
            if not shop:
                raise ValueError(f"门店 {it['shop_id']} 不存在或已删除")
            intro_row = None
            if it["intro_id"]:
                # #456：intro_id 为空表示「不添加视频简介」，跳过 DB 查询
                intro_row = d.query_one(
                    "SELECT content FROM video_intro WHERE id=? AND deleted=0",
                    (it["intro_id"],))
                if not intro_row:
                    raise ValueError(f"简介 {it['intro_id']} 不存在或已删除")
            d.insert("publish_task_item", {
                "task_id": task_id,
                "plan_time": it["plan_time"],
                "account_id": it["account_id"],
                "project_id": it["project_id"],
                "video_id": video["id"],
                "shop_id": it["shop_id"],
                "intro_id": it["intro_id"],
                "intro_snapshot": intro_row["content"] if intro_row else it["intro_content"],
                "topics_snapshot": json.dumps(it["intro_topics"], ensure_ascii=False),
                "declaration": task["declaration"],
                "allow_download": task["allow_download"],
                "status": "waiting",
            })
            d.execute("UPDATE generated_video SET status='occupied' WHERE id=?", (video["id"],))
            inserted += 1
        # #v42：status='running' 时若 started_at 仍为空则写入（first-running-at 语义）——
        # 不能用 update_by_id（仅支持 `?` 占位符），这里直接 execute 配合 COALESCE
        d.execute(
            "UPDATE publish_task SET status='running', "
            "started_at=COALESCE(started_at, ?), "
            "per_account_count=?, update_time=? WHERE id=?",
            (now_str(), inserted // max(len(json.loads(task["account_ids_json"])), 1),
             now_str(), task_id))
    # #confirm-kickoff：事务外立即派发任务下全部 waiting 明细（绕开 plan_time<=now gate）
    try:
        kicked = dispatch_all_waiting(task_id)
        logger.info("[publish] 任务 {} 实时派发 {} 条", task_id, kicked)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[publish] 任务 {} 实时派发失败: {}", task_id, exc)

    return {"task_id": task_id, "item_count": inserted, "running": True}


# ---------- #441 直传 payload 版预览/确认（wizard 临时化） ----------

def _log_payload_snapshot(stage: str, payload: dict) -> None:
    """#449 + #450：把 wizard 上传的 payload（含 task_name / 账号标签 / 项目名 / 店名 / 简介内容）
    打到后端日志。仅日志——排期逻辑完全不依赖这些字段，缺失也无所谓。
    #451 修复——支持读取新形态 snapshots（项目结构 / 店对象 / 简介对象）；
    调用方应在 _normalize_payload 之后调用，否则新形态走下来取不到字段。
    #审查修：用顶部 loguru logger（统一走项目日志 sink），不要 shadow 成 stdlib logging。"""
    # #451：账号 ID / 标签——优先 snapshots（新形态），回退 account_ids（旧形态）
    acc_snaps = payload.get("account_snapshots") or []
    if acc_snaps:
        acc_pairs = [
            (a.get("id") or "", a.get("nickname") or a.get("remark") or "")
            for a in acc_snaps
        ]
    else:
        ids = payload.get("account_ids") or []
        labels = payload.get("account_labels") or []
        acc_pairs = [(a, labels[i] if i < len(labels) else "") for i, a in enumerate(ids)]
    proj_snaps = payload.get("project_snapshots") or []
    if proj_snaps:
        proj_list = proj_snaps
    else:
        proj_list = payload.get("projects") or []
    logger.info(
        "[%s] task=%s | accounts=[%s] | projects=%d",
        stage,
        payload.get("task_name"),
        ", ".join(f"{aid}/{lbl}" for aid, lbl in acc_pairs),
        len(proj_list),
    )
    for i, p in enumerate(proj_list, 1):
        if "shops" in p:  # 新形态
            shop_ids = [s.get("id", "") for s in (p.get("shops") or [])]
            shop_names = [s.get("name", "") for s in (p.get("shops") or [])]
            shop_cities = [s.get("city", "") for s in (p.get("shops") or [])]
            intro_ids = [it.get("id", "") for it in (p.get("intros") or [])]
            intro_contents = [it.get("content", "") for it in (p.get("intros") or [])]
            proj_id = p.get("id", "")
            proj_title = p.get("title", "")
        else:  # 旧形态
            shop_ids = p.get("shop_ids") or []
            shop_names = p.get("shop_names") or []
            shop_cities = p.get("shop_cities") or []
            intro_ids = p.get("intro_ids") or []
            intro_contents = p.get("intro_contents") or []
            proj_id = p.get("project_id", "")
            proj_title = p.get("project_title", "")
        logger.info(
            "[%s] project#%d id=%s title=%s shops=%d intros=%d",
            stage, i, proj_id, proj_title, len(shop_ids), len(intro_ids),
        )
        for j, sid in enumerate(shop_ids):
            logger.info(
                "[%s]   shop[%d] %s / name=%s / city=%s",
                stage, j, sid,
                shop_names[j] if j < len(shop_names) else "",
                shop_cities[j] if j < len(shop_cities) else "",
            )
        for j, iid in enumerate(intro_ids):
            content = intro_contents[j] if j < len(intro_contents) else ""
            logger.info("[%s]   intro[%d] %s / content=%s", stage, j, iid, content)


def _resolve_projects_payload(payload: dict) -> tuple[list, list]:
    """#441：从 payload.projects 构造 ProjectSpec 列表 + 收集 warnings（draft vs DB 不一致）。
    纯函数 + 只读；不写 DB，不依赖 task_id。
    入参 payload: DraftRequest 序列化后的 dict，含 projects=[{project_id, shop_ids, intro_ids}]
    返回 (projects, warnings)
    - projects: list[ProjectSpec] 给 build_schedule 用
    - warnings: list[{project_id, stale_in_draft, extra_in_draft}]
    """
    projects: list[ProjectSpec] = []
    warnings: list[dict] = []
    projects_payload = payload.get("projects") or []
    for p in projects_payload:
        project_id = p["project_id"]
        # shop_ids: None 才 fallback 到 DB（兼容旧 wizard）；空 list 报错（前端 canAdvance 阻止）
        shop_ids_payload = p.get("shop_ids")
        if shop_ids_payload is None:
            shop_ids = _resolve_project_shops(project_id)
        else:
            shop_ids = shop_ids_payload
            # #445：可发布集合 = project_shop 绑定 ∩ is_added=1 ∩ is_cps=1
            publishable_shops = set(_resolve_project_shops(project_id))
            draft_set = set(shop_ids)
            # stale_in_draft：draft 勾的但当前不可用（被取消加库 / 无佣金 / 解除绑定）
            stale_in_draft = [s for s in shop_ids if s not in publishable_shops]
            # extra_in_draft：draft 勾的店曾绑定项目但已被彻底移除（区别于 stale_in_draft 的业务失效）
            raw_binding = _resolve_raw_project_shop_ids(project_id)
            extra_in_draft = [s for s in shop_ids if s not in raw_binding]
            if stale_in_draft or extra_in_draft:
                warnings.append({
                    "project_id": project_id,
                    "stale_in_draft": stale_in_draft,
                    "extra_in_draft": extra_in_draft,
                })
        # intro_ids 同样
        intro_ids_payload = p.get("intro_ids")
        if intro_ids_payload is None:
            intro_ids = [r["id"] for r in _resolve_intros(project_id)]
        else:
            intro_ids = intro_ids_payload
        if not shop_ids:
            raise ValueError(f"项目 {project_id} 门店列表为空")
        # #456：视频简介允许为空（前端不勾/不输入 → intro_ids=[]），
        # 下方 intros 循环自然得到 intros=()，下游排期层用占位 intro_id='' 渲染
        # 构造 IntroSpec（取 content + topics）
        # #453：DB 优先（list 模式 / 含 enabled=0），DB miss 时回退到 payload 内的 content。
        # manual 模式：intro_id 是前端稳定 uuid `intro_<pid>_<idx>`，DB 必 miss，全走 fallback。
        # list 模式：intro_id 是 video_intro.id，正常命中；万一 enabled=0 也命中（不再被 _resolve_intros 过滤）。
        from app.services import publish_intro_service
        all_intros_raw = {r["id"]: r for r in publish_intro_service.list_intros(project_id)}
        payload_intro_contents = p.get("intro_contents") or []
        payload_intro_topics = p.get("intro_topics") or []
        intros: list[IntroSpec] = []
        for idx, iid in enumerate(intro_ids):
            row = all_intros_raw.get(iid)
            if row and not row.get("deleted"):
                topics = row.get("topics") or []
                intros.append(IntroSpec(id=row["id"], content=row["content"], topics=tuple(topics)))
                continue
            # #453 fallback：manual 模式 / enabled=0（list_intros 仍返回，未删）
            content = payload_intro_contents[idx] if idx < len(payload_intro_contents) else ""
            topics_raw = payload_intro_topics[idx] if idx < len(payload_intro_topics) else ""
            if isinstance(topics_raw, str):
                try:
                    topics_tuple = tuple(json.loads(topics_raw) or [])
                except (json.JSONDecodeError, TypeError):
                    topics_tuple = ()
            elif isinstance(topics_raw, list):
                topics_tuple = tuple(topics_raw)
            else:
                topics_tuple = ()
            if not content:
                # #456：content 为空（manual 模式未传 / list 模式 DB miss 且 payload 未补）→
                # 静默跳过此 intro；intros=() 合法，由排期层用占位 intro_id='' 渲染
                continue
            intros.append(IntroSpec(id=iid, content=content, topics=topics_tuple))
        projects.append(ProjectSpec(
            id=project_id, title="",
            shop_ids=tuple(shop_ids), intros=tuple(intros)))
    return projects, warnings


# ---------- #418 视频目录模式辅助函数 ----------

def _build_video_dir_specs(payload: dict) -> list:
    """把 payload.video_dirs + payload.manual_shops 转成 VideoDirProjectSpec 列表。"""
    from app.services.publish_schedule import VideoDirProjectSpec
    video_dirs_in = payload.get("video_dirs") or []
    manual_shops_in = payload.get("manual_shops") or {}
    if not video_dirs_in:
        raise ValueError("[项目] 视频目录列表不能为空")
    specs = []
    for vd in video_dirs_in:
        abs_path = vd.get("abs_path") or ""
        if not abs_path:
            raise ValueError(f"[项目] 视频目录 {vd.get('id')} 路径为空")
        info = _scan_video_dir(abs_path)
        dir_id = vd.get("id") or abs_path
        shops = manual_shops_in.get(dir_id) or []
        if not shops:
            raise ValueError(f"[门店] 目录 {info['dir_name']} 必须至少输入一家门店")
        shop_names = tuple(s.get("name") for s in shops if s.get("name"))
        if not shop_names:
            raise ValueError(f"[门店] 目录 {info['dir_name']} 门店名称不能为空")
        specs.append(VideoDirProjectSpec(
            id=dir_id,
            title=info["dir_name"],
            abs_path=info["abs_path"],
            video_paths=tuple(info["video_files"]),
            shop_names=shop_names,
        ))
    return specs


def _validate_payload_video_dir(payload: dict) -> None:
    """#418 视频目录模式硬校验（账号 + 时间窗）。"""
    if not payload.get("account_ids"):
        raise ValueError("[账号] 账号列表不能为空")
    d = get_db()
    for aid in payload["account_ids"]:
        acc = d.query_one("SELECT status FROM account WHERE id=? AND deleted=0", (aid,))
        if not acc:
            raise ValueError(f"[账号] 账号 {aid} 不存在")
        if acc["status"] != "normal":
            raise ValueError(f"[账号] 账号 {acc.get('nickname') or aid} 非正常状态")
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    if start_time >= end_time:
        raise ValueError("[时间] 结束时间必须晚于起始时间")


def _preview_video_dir(payload: dict) -> dict:
    """#418 视频目录模式 preview。"""
    # #293：先 normalize 把 account_snapshots → account_ids，否则 video_dir 模式早返回时无 account_ids
    _normalize_payload(payload)
    if not payload.get("account_ids"):
        raise ValueError("[账号] 账号列表不能为空")
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    if payload["daily_limit_mode"] == "global":
        per_acc = ()
        global_lim = int(payload.get("daily_limit_global") or 0)
    else:
        per_acc_raw = payload.get("daily_limit_per_account") or []
        per_acc = tuple((r["account_id"], int(r["limit"])) for r in per_acc_raw)
        global_lim = 0
    # #calc_mode：统一解析三字段（与 _confirm_video_dir / _confirm_task_from_video_dir 共用）
    schedule_mode, fixed_interval_min, balanced_step_min, _ = _resolve_schedule_fields(payload)
    specs = _build_video_dir_specs(payload)
    cfg = VideoDirScheduleConfig(
        account_ids=tuple(payload["account_ids"]),
        video_dirs=tuple(specs),
        daily_limit_mode=payload["daily_limit_mode"],
        daily_limit_global=global_lim,
        daily_limit_per_account=per_acc,
        start_time=start_time,
        end_time=end_time,
        # #calc_mode：三字段透传，与 ScheduleConfig 一致
        schedule_mode=schedule_mode,
        fixed_interval_min=fixed_interval_min,
        balanced_step_min=balanced_step_min,
        # 视频简介-按项目一一对应：payload.manual_intros = {dir_id: [str, ...]}
        manual_intros={
            d.id: tuple(payload.get("manual_intros", {}).get(d.id) or ())
            for d in specs
        },
    )
    try:
        items, stats = build_video_dir_schedule(cfg)
    except ScheduleOverflowError as e:
        # 异常类自带 category（time / video），前端按 category 回退到对应 step
        # message 前缀由异常构造方负责加，confirm 直接透传
        msg = str(e)
        if e.category == "video":
            msg = f"[视频] {msg}"
        else:
            msg = f"[时间] {msg}"
        return {"overflow": True, "type": f"{e.category}_shortage", "message": msg,
                "items": [], "stats": {},
                "validation": {"warnings": [], "has_warnings": False}}
    # 视频目录 id → 目录名 映射（#418：预览项目列显示 dir_name 与 wizard step2 一致）
    dir_name_map = {d.id: d.title for d in cfg.video_dirs}
    return {
        "overflow": False,
        "items": [
            {
                "account_id": it.account_id,
                "project_id": it.project_id,
                "project_title": dir_name_map.get(it.project_id, it.project_id),
                "shop_id": it.shop_name,
                "shop_name": it.shop_name,
                "intro_id": "",
                "intro_content": it.intro_content,
                "intro_topics": [],
                "plan_time": it.plan_time,
                "video_path": it.video_path,
            }
            for it in items
        ],
        "stats": {
            "total": stats.total,
            "by_account": stats.by_account,
            "by_project": stats.by_directory,
        },
        "validation": {"warnings": [], "has_warnings": False},
    }


def _confirm_video_dir(payload: dict) -> dict:
    """#418 视频目录模式 confirm。"""
    _normalize_payload(payload)
    _log_payload_snapshot("confirm-direct-video-dir", payload)
    _validate_payload_video_dir(payload)
    preview = _preview_video_dir(payload)
    if preview.get("overflow"):
        # 错误前缀（[视频]/[时间]）已由 _preview_video_dir 写入 message，直接抛
        raise ValueError(preview["message"])
    items_raw = preview["items"]
    if not items_raw:
        raise ValueError("[项目] 视频目录模式下无可发布明细")
    # 保存 manual_intros 到 JSON（编辑回显 + 历史快照）——按项目一一对应的 dict
    manual_intros_json = json.dumps(payload.get("manual_intros") or {}, ensure_ascii=False)
    d = get_db()
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    daily_limit_mode = payload["daily_limit_mode"]
    if daily_limit_mode == "global":
        global_lim = int(payload.get("daily_limit_global") or 0)
        per_acc = []
    else:
        per_acc = payload.get("daily_limit_per_account") or []
        global_lim = 0
    # #calc_mode：统一解析三字段 + 旧字段 fallback（与 _preview_video_dir / _confirm_task_from_video_dir 共用）
    schedule_mode, fixed_interval_min, balanced_step_min, same_project_interval_min = _resolve_schedule_fields(payload)
    declaration = payload.get("declaration") or _DEFAULT_DECLARATION
    allow_download = bool(payload.get("allow_download"))
    video_dirs_json = json.dumps(payload.get("video_dirs") or [], ensure_ascii=False)
    manual_shops_json = json.dumps(payload.get("manual_shops") or {}, ensure_ascii=False)
    account_ids = payload["account_ids"]
    with d.transaction():
        task_id = d.insert("publish_task", {
            "task_name": payload["task_name"],
            "account_ids_json": json.dumps(account_ids, ensure_ascii=False),
            "project_ids_json": json.dumps([it["project_id"] for it in items_raw], ensure_ascii=False),
            "shop_ids_json": json.dumps([]),
            "per_account_count": 0,
            "interval_minutes": fixed_interval_min if schedule_mode == "fixed" else balanced_step_min,
            "start_time": start_time,
            "end_time": end_time,
            # #calc_mode：透传用户实际选择 + 旧字段 fallback（不再硬编码 DEFAULT，保留用户原值）
            "schedule_mode": schedule_mode,
            "fixed_interval_min": fixed_interval_min,
            "balanced_step_min": balanced_step_min,
            "same_project_interval_min": same_project_interval_min if same_project_interval_min is not None else _DEFAULT_SAME_INTERVAL,
            "diff_project_interval_min": _DEFAULT_DIFF_INTERVAL,
            "daily_limit_mode": daily_limit_mode,
            "daily_limit_global": global_lim,
            "daily_limit_per_account_json": json.dumps(per_acc, ensure_ascii=False),
            "declaration": declaration,
            "allow_download": 1 if allow_download else 0,
            "status": "draft",
            "project_source": "video_dir",
            "video_dirs_json": video_dirs_json,
            "manual_shops_json": manual_shops_json,
            "manual_intros_json": manual_intros_json,
            "projects_payload_json": json.dumps([], ensure_ascii=False),
        })
        for it in items_raw:
            d.insert("publish_task_item", {
                "task_id": task_id,
                "plan_time": it["plan_time"],
                "account_id": it["account_id"],
                "project_id": it["project_id"],
                "video_id": "",
                "video_path": it["video_path"],
                "shop_id": it["shop_id"],
                "intro_id": "",
                "intro_snapshot": it.get("intro_content") or "",  # #292：写入 manual intro
                "topics_snapshot": "[]",
                "declaration": declaration,
                "allow_download": 1 if allow_download else 0,
                "source": "video_dir",
                "status": "waiting",
            })
        # #v42：首次进入 running 时写 started_at；用 execute + COALESCE（update_by_id 仅支持 ? 占位符）
        d.execute(
            "UPDATE publish_task SET status='running', "
            "started_at=COALESCE(started_at, ?), "
            "per_account_count=?, update_time=? WHERE id=?",
            (now_str(), len(items_raw) // max(len(account_ids), 1), now_str(), task_id))
    # #confirm-kickoff：事务外立即派发任务下全部 waiting 明细（绕开 plan_time<=now gate）
    # 失败仅日志，不回滚 confirm 的成功包
    try:
        kicked = dispatch_all_waiting(task_id)
        logger.info("[publish] 视频目录任务 {} 实时派发 {} 条", task_id, kicked)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[publish] 视频目录任务 {} 实时派发失败: {}", task_id, exc)
    logger.info("[publish] 视频目录任务 {} 落库完成 items={}", task_id, len(items_raw))
    return {"task_id": task_id, "item_count": len(items_raw), "running": True}


def _normalize_payload(payload: dict) -> None:
    """#450：把新形态（project_snapshots + account_snapshots）规整成旧形态字段，
    下游 _soft_validate_payload / _validate_payload / _resolve_projects_payload 不需感知。
    - account_snapshots → account_ids + account_labels（保持顺序）
    - project_snapshots[].shops → projects[].shop_ids + shop_names + shop_cities + shop_poi_ids
    - project_snapshots[].intros（content 文本）→ projects[].intro_ids（每条前端 uuid）+ intro_contents + intro_topics
    #451 简化：pydantic v2 dump 后必为 dict，isinstance 死分支去掉，统一 .get() 兜底。
    """
    account_snaps = payload.get("account_snapshots") or []
    if account_snaps and not payload.get("account_ids"):
        payload["account_ids"] = [a.get("id", "") for a in account_snaps]
        payload["account_labels"] = [
            a.get("nickname") or a.get("remark") or ""
            for a in account_snaps
        ]
    proj_snaps = payload.get("project_snapshots") or []
    if proj_snaps and not payload.get("projects"):
        normalized = []
        for pd in proj_snaps:
            shops = pd.get("shops") or []
            intros = pd.get("intros") or []
            normalized.append({
                "project_id": pd.get("id", ""),
                "project_title": pd.get("title") or "",
                "shop_ids": [s.get("id", "") for s in shops],
                "shop_names": [s.get("name", "") for s in shops],
                "shop_cities": [s.get("city", "") for s in shops],
                "shop_poi_ids": [s.get("poi_id", "") for s in shops],
                # #450：intro_ids 用每条 intro 的 id（前端 uuid）；intro_contents 用 content；
                # intro_topics 用 JSON 字符串，便于日志读
                "intro_ids": [i.get("id", "") for i in intros],
                "intro_contents": [i.get("content", "") for i in intros],
                "intro_topics": [
                    json.dumps(i.get("topics") or [], ensure_ascii=False)
                    for i in intros
                ],
            })
        payload["projects"] = normalized


def _soft_validate_payload(payload: dict) -> None:
    """#445：软校验（preview-direct 阶段使用）。
    仅校验 wizard 必填项 + 账号存在 + 项目存在 + 时间窗；店/intro 业务属性不硬校验（仅 warnings）。
    错误仍带 step prefix（[账号]/[项目]/[时间]），方便前端回退。
    """
    d = get_db()
    # 1. 账号
    account_ids = payload.get("account_ids") or []
    if not account_ids:
        raise ValueError("[账号] 账号列表不能为空")
    marks = ",".join("?" * len(account_ids))
    acc_rows = d.query_all(
        f"SELECT id, status FROM account WHERE id IN ({marks}) AND deleted=0",
        tuple(account_ids))
    acc_status_map = {r["id"]: (r["status"] or "normal") for r in acc_rows}
    for aid in account_ids:
        if aid not in acc_status_map:
            raise ValueError(f"[账号] 账号 {aid} 不存在或已删除")
        if acc_status_map[aid] != "normal":
            raise ValueError(f"[账号] 账号 {aid} 非正常状态，请先处理登录态")
    # 2. 项目
    projects_payload = payload.get("projects") or []
    if not projects_payload:
        raise ValueError("[项目] 项目列表不能为空")
    seen_proj = set()
    for p in projects_payload:
        pid = p["project_id"]
        if pid in seen_proj:
            raise ValueError(f"[项目] 项目 {pid} 重复")
        seen_proj.add(pid)
    proj_marks = ",".join("?" * len(seen_proj))
    proj_rows = d.query_all(
        f"SELECT id, status FROM project WHERE id IN ({proj_marks}) AND deleted=0",
        tuple(seen_proj))
    proj_status_map = {r["id"]: (r["status"] or "normal") for r in proj_rows}
    for pid in seen_proj:
        if pid not in proj_status_map:
            raise ValueError(f"[项目] 项目 {pid} 不存在或已删除")
        if proj_status_map[pid] != "normal":
            raise ValueError(f"[项目] 项目 {pid} 已停用，请重新选择")
    # 3. 时间窗
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    try:
        start_dt = _parse_flexible(start_time)
        end_dt = _parse_flexible(end_time)
    except ValueError as e:
        raise ValueError(f"[时间] {e}") from None
    if start_dt.date() != end_dt.date():
        raise ValueError("[时间] 起始/结束时间必须同一天")
    if end_dt <= start_dt:
        raise ValueError("[时间] 结束时间必须晚于起始时间")


def _validate_payload(payload: dict) -> None:
    """#441 + #445：硬校验（confirm-direct 阶段使用）。
    错误信息加 step prefix 让前端决定回退到哪一步：
    - [账号] xxx → step 1
    - [项目] xxx → step 2
    - [门店] xxx → step 3
    - [简介] xxx → step 4
    - [时间] xxx → step 5
    - [排期] xxx → step 5
    """
    d = get_db()
    # 1. 账号（#442：批量查 + 一次性校验 status，None 视为 normal 兜底）
    account_ids = payload.get("account_ids") or []
    if not account_ids:
        raise ValueError("[账号] 账号列表不能为空")
    marks = ",".join("?" * len(account_ids))
    acc_rows = d.query_all(
        f"SELECT id, status FROM account WHERE id IN ({marks}) AND deleted=0",
        tuple(account_ids))
    acc_status_map = {r["id"]: (r["status"] or "normal") for r in acc_rows}
    for aid in account_ids:
        if aid not in acc_status_map:
            raise ValueError(f"[账号] 账号 {aid} 不存在或已删除")
        if acc_status_map[aid] != "normal":
            raise ValueError(f"[账号] 账号 {aid} 非正常状态，请先处理登录态")
    # 2. 项目（#442：批量查）
    projects_payload = payload.get("projects") or []
    if not projects_payload:
        raise ValueError("[项目] 项目列表不能为空")
    seen_proj = set()
    for p in projects_payload:
        pid = p["project_id"]
        if pid in seen_proj:
            raise ValueError(f"[项目] 项目 {pid} 重复")
        seen_proj.add(pid)
    proj_marks = ",".join("?" * len(seen_proj))
    proj_rows = d.query_all(
        f"SELECT id, status FROM project WHERE id IN ({proj_marks}) AND deleted=0",
        tuple(seen_proj))
    proj_status_map = {r["id"]: (r["status"] or "normal") for r in proj_rows}
    for pid in seen_proj:
        if pid not in proj_status_map:
            raise ValueError(f"[项目] 项目 {pid} 不存在或已删除")
        if proj_status_map[pid] != "normal":
            raise ValueError(f"[项目] 项目 {pid} 已停用，请重新选择")
    # 3. 门店（#442 批量查 + #444 加库/佣金校验）
    # 收集所有候选 sid 去重后一次查表
    candidate_shop_ids: set[str] = set()
    for p in projects_payload:
        for sid in (p.get("shop_ids") or []):
            candidate_shop_ids.add(sid)
    # 批量查 deleted / is_added_to_library / is_cps
    shop_meta_map: dict[str, dict] = {}
    if candidate_shop_ids:
        sm = ",".join("?" * len(candidate_shop_ids))
        shop_rows = d.query_all(
            f"SELECT id, deleted, is_added_to_library, is_cps FROM shop WHERE id IN ({sm})",
            tuple(candidate_shop_ids))
        shop_meta_map = {r["id"]: r for r in shop_rows}
    # db_shops 用过滤后的列表（#444 list_project_shops 已 is_added=1 + is_cps=1）
    for p in projects_payload:
        pid = p["project_id"]
        db_shops = set(_resolve_project_shops(pid))
        shop_ids = p.get("shop_ids") or []
        if not shop_ids:
            raise ValueError(f"[门店] 项目 {pid} 至少选择 1 家门店")
        for sid in shop_ids:
            if sid not in db_shops:
                raise ValueError(f"[门店] 门店 {sid} 未绑定到项目 {pid}（或未加库/无佣金），请回第 3 步重新选择")
            meta = shop_meta_map.get(sid)
            if not meta or meta["deleted"]:
                raise ValueError(f"[门店] 门店 {sid} 不存在或已删除，请回第 3 步重新选择")
            # #444：业务硬约束——未加库 / 无佣金的店不能发布（与 #414 drawer UI 过滤对齐）
            if not meta["is_added_to_library"]:
                raise ValueError(f"[门店] 门店 {sid} 未加库，请回第 3 步选择已加库的门店")
            if not meta["is_cps"]:
                raise ValueError(f"[门店] 门店 {sid} 无佣金，请回第 3 步选择有佣金的门店")
    # 4. 简介（#442：批量查 deleted/enabled）
    candidate_intro_ids: set[str] = set()
    for p in projects_payload:
        for iid in (p.get("intro_ids") or []):
            candidate_intro_ids.add(iid)
    intro_meta_map: dict[str, dict] = {}
    if candidate_intro_ids:
        im = ",".join("?" * len(candidate_intro_ids))
        intro_rows = d.query_all(
            f"SELECT id, project_id, deleted, enabled FROM video_intro WHERE id IN ({im})",
            tuple(candidate_intro_ids))
        intro_meta_map = {r["id"]: r for r in intro_rows}
    for p in projects_payload:
        pid = p["project_id"]
        enabled_intro_ids = {r["id"] for r in _resolve_intros(pid)}
        intro_ids = p.get("intro_ids") or []
        intro_contents = p.get("intro_contents") or []
        # #456：视频简介允许为空（前端勾选空数组 = 不添加视频简介），不再 raise
        for idx, iid in enumerate(intro_ids):
            if iid in enabled_intro_ids:
                continue
            # #453：manual 模式 intro_id 是前端稳定 uuid，不在 video_intro 表——
            # 用 payload 的 intro_contents[idx] 兜底放行；保证 content 非空
            manual_content = intro_contents[idx] if idx < len(intro_contents) else ""
            if manual_content and manual_content.strip():
                continue
            row = intro_meta_map.get(iid)
            if not row or row["deleted"]:
                raise ValueError(f"[简介] 简介 {iid} 不存在或已删除，请回第 4 步重新选择")
            if not row["enabled"]:
                raise ValueError(f"[简介] 简介 {iid} 已停用，请回第 4 步重新选择")
            raise ValueError(f"[简介] 简介 {iid} 不在项目 {pid} 下，请回第 4 步重新选择")
    # 5. per_account 模式覆盖校验（#442：per_account 必须覆盖所有 account_ids）
    if payload.get("daily_limit_mode") == "per_account":
        per_acc_raw = payload.get("daily_limit_per_account") or []
        per_acc_ids = {r.get("account_id") for r in per_acc_raw if r.get("account_id")}
        missing = [aid for aid in account_ids if aid not in per_acc_ids]
        if missing:
            raise ValueError(
                f"[排期] 分账号模式下账号 {missing} 未指定每天上限"
            )
        extra = [aid for aid in per_acc_ids if aid not in acc_status_map]
        if extra:
            raise ValueError(
                f"[账号] 分账号模式下账号 {list(extra)} 不在发布账号列表中"
            )
        for r in per_acc_raw:
            lim = int(r.get("limit") or 0)
            if lim < 1:
                raise ValueError(
                    f"[排期] 账号 {r.get('account_id')} 每天上限必须 ≥ 1"
                )
    elif payload.get("daily_limit_mode") == "global":
        gl = int(payload.get("daily_limit_global") or 0)
        if gl < 1:
            raise ValueError("[排期] 全局每天上限必须 ≥ 1")
    else:
        raise ValueError(f"[排期] daily_limit_mode 非法：{payload.get('daily_limit_mode')!r}")
    # 5. 时间窗
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    try:
        start_dt = _parse_flexible(start_time)
        end_dt = _parse_flexible(end_time)
    except ValueError as e:
        raise ValueError(f"[时间] {e}") from None
    if start_dt.date() != end_dt.date():
        raise ValueError("[时间] 起始/结束时间必须同一天")
    if end_dt <= start_dt:
        raise ValueError("[时间] 结束时间必须晚于起始时间")


def preview_task_direct(payload: dict) -> dict:
    """#441 + #445：纯预览（不入库）。直接接 DraftRequest 序列化 payload。
    流程：_soft_validate_payload → _resolve_projects_payload（收集 stale_in_draft warnings）→
          构造 ScheduleConfig → build_schedule
    返回结构与 preview_task 兼容（items/stats/validation/overflow）。
    preview 阶段不硬校验店/intro 业务属性（让 warnings 暴露），confirm 阶段才硬阻塞。

    #418 视频目录模式：旁路 projects/build_schedule，调 scan_video_dir + build_video_dir_schedule。
    """
    # #418：视频目录模式早返回（不走 _normalize_payload / _resolve_projects_payload）
    if payload.get("project_source") == "video_dir":
        return _preview_video_dir(payload)
    # #451：先规整快照形态再打日志（否则新形态走下来取不到 projects/intro_contents）
    _normalize_payload(payload)
    # #449：把 wizard 传来的 task_name / 账号标签 / 项目名称 / 店名称 / 简介内容快照打到日志
    _log_payload_snapshot("preview-direct", payload)
    # 软校验（账号存在/项目存在/时间窗；店/intro 业务属性不卡）
    _soft_validate_payload(payload)
    # 构造 ProjectSpec + 收集 warnings
    projects, warnings = _resolve_projects_payload(payload)
    # 时间窗 + 排期配置
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    if payload["daily_limit_mode"] == "global":
        per_acc = ()
        global_lim = int(payload.get("daily_limit_global") or 0)
    else:
        per_acc_raw = payload.get("daily_limit_per_account") or []
        per_acc = tuple((r["account_id"], int(r["limit"])) for r in per_acc_raw)
        global_lim = 0
    cfg = ScheduleConfig(
        account_ids=tuple(payload["account_ids"]),
        projects=tuple(projects),
        daily_limit_mode=payload["daily_limit_mode"],
        daily_limit_global=global_lim,
        daily_limit_per_account=per_acc,
        start_time=start_time,
        end_time=end_time,
        # #calc_mode：优先新字段，旧字段 fallback（is None 避免 0 陷阱）
        schedule_mode=payload.get("schedule_mode") if payload.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE,
        fixed_interval_min=int(payload["fixed_interval_min"]) if payload.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL,
        balanced_step_min=(
            int(payload["balanced_step_min"]) if payload.get("balanced_step_min") is not None
            else (int(payload["same_project_interval_min"]) if payload.get("same_project_interval_min") is not None
                  else _DEFAULT_BALANCED_STEP)
        ),
        same_project_interval_min=payload.get("same_project_interval_min"),
        diff_project_interval_min=payload.get("diff_project_interval_min"),
    )
    try:
        items, stats = build_schedule(cfg)
    except ScheduleOverflowError as e:
        return {"overflow": True, "message": str(e),
                "items": [], "stats": {},
                "validation": {"warnings": warnings, "has_warnings": bool(warnings)}}
    return {
        "overflow": False,
        "items": [
            {
                "account_id": it.account_id,
                "project_id": it.project_id,
                "shop_id": it.shop_id,
                "intro_id": it.intro_id,
                "intro_content": it.intro_content,
                "intro_topics": list(it.intro_topics),
                "plan_time": it.plan_time,
            }
            for it in items
        ],
        "stats": {
            "total": stats.total,
            "by_account": stats.by_account,
            "by_project": stats.by_project,
        },
        "validation": {"warnings": warnings, "has_warnings": bool(warnings)},
    }


def confirm_task_direct(payload: dict) -> dict:
    """#441：直接接 payload 一次性入库（不需要先 save_draft）。
    事务内：硬校验 → INSERT publish_task → build_schedule → 水量校验 → 占成品 + 落明细 + status='running'。

    #418 视频目录模式：旁路 project_pool/build_schedule，走 scan + build_video_dir_schedule。
    """
    # #418：视频目录模式早返回
    if payload.get("project_source") == "video_dir":
        return _confirm_video_dir(payload)
    # #451：先规整快照形态再打日志
    _normalize_payload(payload)
    # #449：快照日志（含 task_name / 账号标签 / 项目名 / 店名 / 简介内容）便于排查
    _log_payload_snapshot("confirm-direct", payload)
    d = get_db()
    # 1. 硬校验（任一不通过直接抛 ValueError，前端按 prefix 决定回退）
    _validate_payload(payload)
    # 2. 构造 + 收集 warnings
    projects, warnings = _resolve_projects_payload(payload)
    if warnings:
        # 阻塞：让前端回 step 3 重选门店
        raise ValueError(
            f"[门店] {len(warnings)} 个项目门店配置已变更，请回第 3 步重新选择"
        )
    # 3. 解析 daily_limit
    if payload["daily_limit_mode"] == "global":
        per_acc = ()
        global_lim = int(payload.get("daily_limit_global") or 0)
    else:
        per_acc_raw = payload.get("daily_limit_per_account") or []
        per_acc = tuple((r["account_id"], int(r["limit"])) for r in per_acc_raw)
        global_lim = 0
    # 4. 时间窗
    default_start, default_end = _default_start_end()
    start_time = payload.get("start_time") or default_start
    end_time = payload.get("end_time") or default_end
    # 4.5 #calc_mode 解析三个新字段（落库时复用，避免在事务块内重新读 payload）
    schedule_mode = payload.get("schedule_mode") if payload.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE
    fixed_interval_min = int(payload["fixed_interval_min"]) if payload.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL
    balanced_step_min = (
        int(payload["balanced_step_min"]) if payload.get("balanced_step_min") is not None
        else (int(payload["same_project_interval_min"]) if payload.get("same_project_interval_min") is not None
              else _DEFAULT_BALANCED_STEP)
    )
    # 5. 构造 ScheduleConfig + build_schedule
    cfg = ScheduleConfig(
        account_ids=tuple(payload["account_ids"]),
        projects=tuple(projects),
        daily_limit_mode=payload["daily_limit_mode"],
        daily_limit_global=global_lim,
        daily_limit_per_account=per_acc,
        start_time=start_time,
        end_time=end_time,
        # #calc_mode：优先新字段，旧字段 fallback（is None 避免 0 陷阱）
        schedule_mode=payload.get("schedule_mode") if payload.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE,
        fixed_interval_min=int(payload["fixed_interval_min"]) if payload.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL,
        balanced_step_min=(
            int(payload["balanced_step_min"]) if payload.get("balanced_step_min") is not None
            else (int(payload["same_project_interval_min"]) if payload.get("same_project_interval_min") is not None
                  else _DEFAULT_BALANCED_STEP)
        ),
        same_project_interval_min=payload.get("same_project_interval_min"),
        diff_project_interval_min=payload.get("diff_project_interval_min"),
    )
    try:
        items, stats = build_schedule(cfg)
    except ScheduleOverflowError as e:
        raise ValueError(f"[时间] {e}") from None
    needed = len(items)
    project_ids_set = {it.project_id for it in items}
    account_ids_json = json.dumps(payload["account_ids"], ensure_ascii=False)
    daily_limit_per_account_json = json.dumps(
        [{"account_id": aid, "limit": lim} for aid, lim in per_acc],
        ensure_ascii=False)
    projects_payload_json = json.dumps(
        [{"project_id": p.id, "shop_ids": list(p.shop_ids), "intro_ids": [it.id for it in p.intros]}
         for p in projects], ensure_ascii=False)
    # shop_ids_json 用 dict.fromkeys 保序（按勾选顺序去重；不影响实际派发）
    shop_ids_ordered = list(dict.fromkeys(
        sid for p in projects for sid in p.shop_ids))
    shop_ids_json = json.dumps(shop_ids_ordered, ensure_ascii=False)
    # 同样声明取快照（事务内外一致即可）
    declaration = payload.get("declaration") or _DEFAULT_DECLARATION
    allow_download = bool(payload.get("allow_download"))
    # 6. 事务内：水量校验 → INSERT publish_task → 占成品 → 落明细 → status='running'
    # #442：水量 COUNT 必须与占成品在同一事务上下文，避免两台客户端同时 confirm-direct
    # 各自算到 available=N 都进入事务 → 超量占用。
    # SQLite WAL + d.transaction 串行化保证：事务内 COUNT 与 cursor 预加载同一事务上下文，原子。
    with d.transaction():
        # 6.1 水量校验（事务内）
        marks = ",".join("?" * len(project_ids_set))
        available = d.query_one(
            f"SELECT COUNT(*) AS c FROM generated_video WHERE project_id IN ({marks}) AND status='idle'",
            tuple(project_ids_set))["c"]
        if available < needed:
            raise ValueError(
                f"可用水量不足：需 {needed} 条，可用 {available} 条，缺 {needed - available} 条。"
                f"建议：① 减少「发布数量」；② 去「创作中心」生成更多成品；③ 取消/删除其他 draft 释放成品占用"
            )
        # 6.2 INSERT publish_task (临时 draft 状态，事务结束前 UPDATE 为 running)
        task_id = d.insert("publish_task", {
            "task_name": payload["task_name"],
            "account_ids_json": account_ids_json,
            "project_ids_json": json.dumps([p.id for p in projects], ensure_ascii=False),
            "shop_ids_json": shop_ids_json,
            "per_account_count": 0,  # 事务末尾 UPDATE 覆盖
            "interval_minutes": fixed_interval_min if schedule_mode == "fixed" else balanced_step_min,  # legacy 列兼容（#calc_mode：与 save_draft 一致）
            "parallel_strategy": "serial",  # legacy 列兼容
            "title_topic_mode": "random",  # legacy 列兼容
            "shop_assign_mode": "round",  # legacy 列兼容
            "projects_payload_json": projects_payload_json,
            "daily_limit_mode": payload["daily_limit_mode"],
            "daily_limit_global": global_lim,
            "daily_limit_per_account_json": daily_limit_per_account_json,
            "start_time": start_time,
            "end_time": end_time,
            "same_project_interval_min": int(payload["same_project_interval_min"]) if payload.get("same_project_interval_min") is not None else _DEFAULT_SAME_INTERVAL,
            "diff_project_interval_min": int(payload["diff_project_interval_min"]) if payload.get("diff_project_interval_min") is not None else _DEFAULT_DIFF_INTERVAL,
            # #calc_mode 新字段（落库 + 兼容读；is None 避免 0 陷阱）
            "schedule_mode": payload.get("schedule_mode") if payload.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE,
            "fixed_interval_min": int(payload["fixed_interval_min"]) if payload.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL,
            "balanced_step_min": (
                int(payload["balanced_step_min"]) if payload.get("balanced_step_min") is not None
                else (int(payload["same_project_interval_min"]) if payload.get("same_project_interval_min") is not None
                      else _DEFAULT_BALANCED_STEP)
            ),
            "declaration": declaration,
            "allow_download": 1 if allow_download else 0,
            "status": "draft",  # 事务内临时状态；最终落 running
        })
        # 6.3 预加载每个项目的成品池（事务内 + 紧跟 COUNT 保证一致）
        cursor: dict[str, list[dict]] = {}
        for pid in project_ids_set:
            cursor[pid] = d.query_all(
                "SELECT * FROM generated_video WHERE project_id=? AND status='idle' ORDER BY create_time",
                (pid,))
        # 6.4 落明细 + 占成品
        inserted = 0
        for it in items:
            pool = cursor.get(it.project_id) or []
            if not pool:
                raise ValueError(f"项目 {it.project_id} 成品不足")
            video = pool.pop(0)
            shop_row = d.query_one("SELECT * FROM shop WHERE id=? AND deleted=0", (it.shop_id,))
            if not shop_row:
                raise ValueError(f"门店 {it.shop_id} 不存在或已删除")
            # #456：占位条目 intro_id='' → 短路 DB 查询，直接用 IntroSpec.content 兜底
            if it.intro_id:
                intro_row = d.query_one(
                    "SELECT content FROM video_intro WHERE id=? AND deleted=0",
                    (it.intro_id,))
            else:
                intro_row = None
            # #450：临时文本模式——前端用 crypto.randomUUID() 作 intro_id，
            # video_intro 表里查不到时直接用 IntroSpec.content 兜底（不抛错）。
            intro_content_snapshot = (
                intro_row["content"] if intro_row else it.intro_content
            )
            d.insert("publish_task_item", {
                "task_id": task_id,
                "plan_time": it.plan_time,
                "account_id": it.account_id,
                "project_id": it.project_id,
                "video_id": video["id"],
                "shop_id": it.shop_id,
                "intro_id": it.intro_id,
                "intro_snapshot": intro_content_snapshot,
                "topics_snapshot": json.dumps(it.intro_topics, ensure_ascii=False),
                "declaration": declaration,
                "allow_download": 1 if allow_download else 0,
                "status": "waiting",
            })
            d.execute("UPDATE generated_video SET status='occupied' WHERE id=?", (video["id"],))
            inserted += 1
        # 6.5 UPDATE status='running'（事务结束前最后一次写）
        # #v42：首次进入 running 时写 started_at；用 execute + COALESCE（update_by_id 仅支持 ? 占位符）
        d.execute(
            "UPDATE publish_task SET status='running', "
            "started_at=COALESCE(started_at, ?), "
            "per_account_count=?, update_time=? WHERE id=?",
            (now_str(), inserted // max(len(payload["account_ids"]), 1),
             now_str(), task_id))
    # #confirm-kickoff：事务外立即派发任务下全部 waiting 明细（绕开 plan_time<=now gate）
    # 失败仅日志，不回滚 confirm 的成功包
    try:
        kicked = dispatch_all_waiting(task_id)
        logger.info("[publish] 任务 {} 实时派发 {} 条", task_id, kicked)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[publish] 任务 {} 实时派发失败: {}", task_id, exc)
    return {"task_id": task_id, "item_count": inserted, "running": True}


def _build_account_labels(account_ids: list[str]) -> list[str]:
    """按 account_ids 顺序查 account 表填 nickname/remark（_log_payload_snapshot 日志用）。

    publish_task 表不存 snapshots（运行时数据），duplicate_task 必须主动拼才能让
    复制后 confirm-direct 日志有可读账号名。
    """
    if not account_ids:
        return []
    d = get_db()
    marks = ",".join("?" * len(account_ids))
    rows = d.query_all(
        f"SELECT id, nickname, remark FROM account WHERE id IN ({marks}) AND deleted=0",
        tuple(account_ids))
    by_id = {r["id"]: r for r in rows}
    return [
        (lambda a: (a.get("nickname") or a.get("remark") or ""))(by_id[aid])
        if aid in by_id else ""
        for aid in account_ids
    ]


def duplicate_task(task_id: str) -> dict:
    """复制任务为向导 payload（#453 改）：不写库，返回等价 DraftRequest 让前端 wizard 回显。

    用户在 wizard 里修改后点确认 → 走 confirm_task_direct 创建新任务（status=running）。

    不限状态：running / paused / draft / finished / cancelled 均可复制。
    源任务不受影响，原 publish_task 行不被改动。

    返回结构（前端 PublishWizardDialog initialPayload 直接消费）：
        {
            "payload": { ...DraftRequest 字段... }
        }
    """
    d = get_db()
    src = d.query_one("SELECT * FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not src:
        raise ValueError("源任务不存在")
    # #453：不再过滤状态（运行中/暂停也可复制——前端 wizard 允许用户在确认前调整时间窗/账号）
    projects_payload = json.loads(src.get("projects_payload_json") or "[]")
    per_acc = json.loads(src.get("daily_limit_per_account_json") or "[]")
    schedule_mode = src.get("schedule_mode") if src.get("schedule_mode") is not None else _DEFAULT_SCHEDULE_MODE
    fixed_interval_min = int(src["fixed_interval_min"]) if src.get("fixed_interval_min") is not None else _DEFAULT_FIXED_INTERVAL
    balanced_step_min = (
        int(src["balanced_step_min"]) if src.get("balanced_step_min") is not None
        else (int(src["same_project_interval_min"]) if src.get("same_project_interval_min") is not None
              else _DEFAULT_BALANCED_STEP)
    )
    payload = {
        "task_name": f"{src['task_name']}_副本",
        "projects": projects_payload,  # 直接给前端，wizard step 3 解码
        "account_ids": json.loads(src["account_ids_json"] or "[]"),
        "daily_limit_mode": src["daily_limit_mode"],
        # #review warning：实时查 account 表填 account_labels（_log_payload_snapshot 日志读这个）；
        # publish_task 表不存 snapshots（运行时数据），必须主动拼才能让预览日志有可读名字
        "account_labels": _build_account_labels(json.loads(src["account_ids_json"] or "[]")),
        "daily_limit_global": src["daily_limit_global"] or 0,
        "daily_limit_per_account": [{"account_id": r["account_id"], "limit": int(r["limit"])} for r in per_acc],
        "start_time": src["start_time"],
        "end_time": src["end_time"],
        "schedule_mode": schedule_mode,
        "fixed_interval_min": fixed_interval_min,
        "balanced_step_min": balanced_step_min,
        "same_project_interval_min": src.get("same_project_interval_min"),
        "diff_project_interval_min": src.get("diff_project_interval_min"),
        "declaration": src["declaration"],
        "allow_download": bool(src["allow_download"]),
        # #418：视频目录模式必备字段
        "project_source": src.get("project_source") or "project",
        "video_dirs": json.loads(src.get("video_dirs_json") or "[]"),
        "manual_shops": json.loads(src.get("manual_shops_json") or "{}"),
        "manual_intros": json.loads(src.get("manual_intros_json") or "{}"),
    }
    return {"payload": payload}


def get_task_detail_ro(task_id: str) -> dict:
    """只读详情（#413）：与 get_task_detail 等价，但语义上不再支持编辑。"""
    return get_task_detail(task_id)


def create_task(task_name: str, project_ids: list[str], account_ids: list[str],
                shop_ids: list[str], per_account_count: int, start_time: str,
                interval_minutes: int, shop_assign_mode: str = "round") -> dict:
    """创建发布任务：预校验 → 生成排期明细 → 取用成品（置已占用）。

    参数:
        start_time: 开始时间 yyyy-MM-dd HH:mm:ss
        interval_minutes: 同账号两条间隔（分钟，≥5）
        shop_assign_mode: round 轮流 / random 随机
    返回:
        {"task_id", "item_count", "items": [排期预览]}
    """
    d = get_db()
    if interval_minutes < 5:
        raise ValueError("发布间隔最小 5 分钟（防风控）")
    # 账号校验（须正常状态）
    for aid in account_ids:
        acc = d.query_one("SELECT status FROM account WHERE id=? AND deleted=0", (aid,))
        if not acc:
            raise ValueError("账号不存在")
        if acc["status"] != "normal":
            raise ValueError("存在非正常状态账号，请先处理登录态")
    # 门店校验（未删除且未排除）
    for sid in shop_ids:
        shop = d.query_one("SELECT mark FROM shop WHERE id=? AND deleted=0", (sid,))
        if not shop or shop["mark"] == "excluded":
            raise ValueError("门店不存在或已排除")
    # 水量预校验（F-05-R1）
    total_need = per_account_count * len(account_ids)
    marks = ",".join("?" * len(project_ids))
    available = d.query_one(
        f"SELECT COUNT(*) AS c FROM generated_video WHERE project_id IN ({marks}) AND status='idle'",
        tuple(project_ids))["c"]
    if available < total_need:
        raise ValueError(f"可用水量不足：需 {total_need} 条，可用 {available} 条，缺 {total_need - available} 条")

    # 取用成品（按项目顺序，即时置已占用）
    videos: list[dict] = []
    for tid in project_ids:
        rows = d.query_all(
            "SELECT * FROM generated_video WHERE project_id=? AND status='idle' ORDER BY create_time",
            (tid,))
        videos.extend(rows)
    videos = videos[:total_need]

    # 生成排期
    task_id = d.insert("publish_task", {
        "task_name": task_name,
        "project_ids_json": json.dumps(project_ids),
        "account_ids_json": json.dumps(account_ids),
        "shop_ids_json": json.dumps(shop_ids),
        "per_account_count": per_account_count,
        "start_time": start_time,
        "interval_minutes": interval_minutes,
        "shop_assign_mode": shop_assign_mode,
        "status": "running",
    })
    # #420：兼容无秒格式（与 publish_schedule._parse 一致）
    start_dt = _parse_flexible(start_time)
    items: list[dict] = []
    idx = 0
    for ai, account_id in enumerate(account_ids):
        for i in range(per_account_count):
            video = videos[idx]
            idx += 1
            # 门店分配：round 轮流 / random 随机
            if shop_assign_mode == "random":
                shop_id = random.choice(shop_ids)
            else:
                shop_id = shop_ids[(ai * per_account_count + i) % len(shop_ids)]
            plan_time = (start_dt + timedelta(minutes=interval_minutes * i)).strftime("%Y-%m-%d %H:%M:%S")
            item_id = d.insert("publish_task_item", {
                "task_id": task_id, "plan_time": plan_time,
                "account_id": account_id, "project_id": video["project_id"],
                "video_id": video["id"], "shop_id": shop_id, "status": "waiting",
            })
            # 成品置已占用（generated_video 无 update_time 列，直接 execute）
            d.execute("UPDATE generated_video SET status='occupied' WHERE id=?", (video["id"],))
            items.append({"item_id": item_id, "plan_time": plan_time, "account_id": account_id,
                          "video_id": video["id"], "shop_id": shop_id})
    return {"task_id": task_id, "item_count": len(items), "items": items}


def list_tasks(page: int = 1, page_size: int = 20,
                status: str | None = None,
                account_id: str | None = None) -> dict:
    """任务列表（含进度汇总）。
    C3：按状态过滤；#454：按账号过滤——只返回含该账号明细的任务。
    实现：用 EXISTS 子查命中 publish_task_item（即使任务本身已无 account_ids_json 也能命中）。"""
    d = get_db()
    where = "WHERE t.deleted=0"
    params: tuple = ()
    if status:
        where += " AND t.status=?"
        params = (status,)
    if account_id:
        # #454：任务只要有过该账号的明细即命中；status filter + account filter 叠加
        where += " AND EXISTS (SELECT 1 FROM publish_task_item i WHERE i.task_id=t.id AND i.account_id=?)"
        params = params + (account_id,)
    page_data = d.query_page(
        f"""SELECT t.*,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id AND i.status='success') AS success_count_real,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id AND i.status='failed') AS fail_count_real,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id) AS item_total
           FROM publish_task t {where} ORDER BY t.create_time DESC""",
        params, page, page_size)
    # #bugfix：派生 display_status——finished 但有失败明细 → 'partial'（前端徽标变红/黄）
    # 部分失败是「成功 N + 失败 M + 总 K」中最直观的失败标志，比 status='finished' 单字段更有信息量
    for row in page_data.get("list", []):
        row["display_status"] = _derive_display_status(row)
    return page_data


def _derive_display_status(task: dict) -> str:
    """派生状态显示值（前端徽标用）。

    规则：
    - 终态 finished 且有失败明细 → 'partial'（区分纯成功 vs 含失败）
    - 终态 cancelled 但有成功明细 → 'partial'（区分纯取消 vs 部分成功）
    - 其他状态 → 原值
    """
    status = task.get("status") or ""
    fail = task.get("fail_count_real") or 0
    success = task.get("success_count_real") or 0
    if status == "finished" and fail > 0:
        return "partial"
    if status == "cancelled" and success > 0:
        return "partial"
    return status


def get_task_detail(task_id: str) -> dict:
    """任务详情 + 全部明细（#413 真机 bug 修复：补 real count）。"""
    d = get_db()
    task = d.query_one(
        """SELECT t.*,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id AND i.status='success') AS success_count_real,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id AND i.status='failed') AS fail_count_real,
                  (SELECT COUNT(*) FROM publish_task_item i WHERE i.task_id=t.id) AS item_total
           FROM publish_task t WHERE t.id=? AND t.deleted=0""",
        (task_id,))
    if not task:
        raise ValueError("任务不存在")
    items = d.query_all(
        """SELECT i.*, a.nickname AS account_nickname, a.remark AS account_remark,
                  s.name AS shop_name, v.file_path AS video_path,
                  v.combination_key AS video_combination_key,
                  COALESCE(p.title, json_extract(vd.value, '$.dir_name')) AS video_title
           FROM publish_task_item i
           LEFT JOIN account a ON i.account_id=a.id
           LEFT JOIN shop s ON i.shop_id=s.id
           LEFT JOIN generated_video v ON i.video_id=v.id
           LEFT JOIN project p ON i.project_id=p.id
           LEFT JOIN publish_task t ON t.id=i.task_id
           LEFT JOIN json_each(t.video_dirs_json) vd ON json_extract(vd.value, '$.id') = i.project_id
           WHERE i.task_id=? ORDER BY i.plan_time""",
        (task_id,))
    return {**task, "items": items, "display_status": _derive_display_status(task)}


def toggle_task(task_id: str, paused: bool) -> dict:
    """任务暂停/恢复（暂停期间到期明细顺延）。

    #bugfix：恢复时若 worker 已 break 退出，需主动 dispatch_all_waiting 唤醒；
    暂停时只改 task.status，worker 内的 task.status gate 会让 while 循环自然退出。
    """
    d = get_db()
    d.update_by_id("publish_task", task_id, {"status": "paused" if paused else "running"})
    if not paused:
        # 恢复：从 paused → running，worker 可能已 break（task.status gate 触发），主动唤醒
        try:
            n = dispatch_all_waiting(task_id)
            if n:
                logger.info("[toggle_task] 恢复后派发 {} 条 waiting task={}", n, task_id[:8])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[toggle_task] 恢复派发异常: {} task={}", exc, task_id[:8])
    return d.query_one("SELECT id, status FROM publish_task WHERE id=?", (task_id,))


def cancel_task(task_id: str) -> int:
    """取消任务：未执行明细置 cancelled 并释放成品占用。

    #bugfix：同时 request_cancel task_service 让 worker 抛 _TaskCancelled 退出 publish 池。
    否则 worker 仍在 while 循环占用 task_id 引用，后续 dispatch 会再起一个新 worker，
    → publish 池任务翻倍 + info.progress 双写乱
    """
    d = get_db()
    pending = d.query_all(
        "SELECT id, video_id FROM publish_task_item WHERE task_id=? AND status IN ('waiting','suspended')",
        (task_id,))
    for item in pending:
        d.update_by_id("publish_task_item", item["id"], {"status": "cancelled"})
        d.execute("UPDATE generated_video SET status='idle' WHERE id=?", (item["video_id"],))
    d.update_by_id("publish_task", task_id, {"status": "cancelled"})
    # 让 worker 自查 cancel_requested 后停止（_publish_one_task while 循环抛 _TaskCancelled break）
    try:
        from app.services.task_service import task_service
        if task_service.request_cancel(task_id):
            logger.info("[cancel] task={} worker 已请求退出", task_id[:8])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cancel] request_cancel 异常: {} task={}", exc, task_id[:8])
    return len(pending)


def restart_task(task_id: str) -> dict:
    """#优化：cancelled / paused 任务可重启——task 回 running，明细重置回 waiting，重新派发。

    与 retry_task 的区别：
    - restart 处理整个任务（含 cancelled 的 success/failed/cancelled 明细）
    - retry 只复活 failed 明细，其他不动

    规则：
    - cancelled / paused 两态可重启；其他状态走原 cancel/retry 即可
    - paused 任务允许重启：worker 已被 task.status gate 阻在 while 外（cancel_requested 已发），
      拉回 running 后 dispatch 唤起新 worker，旧的 cancel 信号在新 worker 上无效
    - cancelled / failed 明细都改回 waiting 重新派发
    - success 明细保持 success 不动（避免重复发布）
    - 拉回 status='running' 后调 dispatch_all_waiting 主动派发
    """
    d = get_db()
    task = d.query_one("SELECT status FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    # #bugfix：paused 也允许重启——之前只允许 cancelled，但 paused 是 cancel 前的中间态，
    # 用户 UX 期望「暂停后能重启」跟「取消后能重启」一致
    if task["status"] not in ("cancelled", "paused"):
        raise ValueError(f"仅已取消/已暂停任务可重启，当前状态：{task['status']}")

    # 重置 cancelled/failed → waiting（success 不动，避免重复发布）
    reset_count = d.execute(
        """UPDATE publish_task_item SET status='waiting', fail_reason=NULL
           WHERE task_id=? AND status IN ('cancelled','failed')""",
        (task_id,))
    # 重新占成品
    d.execute(
        """UPDATE generated_video SET status='occupied'
           WHERE id IN (SELECT video_id FROM publish_task_item
                        WHERE task_id=? AND video_id IS NOT NULL AND video_id != ''
                          AND status='waiting')""",
        (task_id,))
    # 拉回 running
    d.update_by_id("publish_task", task_id, {"status": "running"})
    # 主动 dispatch 派发全部 waiting
    try:
        n = dispatch_all_waiting(task_id)
        logger.info("[restart_task] {} 复活 {} 条 / 派发 {} 条", task_id[:8], reset_count, n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[restart_task] dispatch 异常: {} task={}", exc, task_id[:8])
        n = 0
    return {"reset": reset_count, "dispatched": n}


def delete_task(task_id: str) -> None:
    """删除任务（draft / finished / cancelled 三态可删；running / paused 不可删）。

    发布记录（publish_record）按 F-05-R10 永久保留。
    draft 状态兜底：先释放 publish_task_item 引用的 generated_video 占用（防
    confirm 后回滚 draft 留下的 occupied 永不归还），再硬删明细 + 软删 task。
    finished/cancelled 状态下所有明细已是 success/failed/cancelled 终态，video
    已自然归还，无须再释放。
    """
    d = get_db()
    task = d.query_one("SELECT status FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("draft", "finished", "cancelled"):
        raise ValueError("仅 draft / 已终态任务可删除")
    # draft 状态兜底释放视频占用：仅释放非空 video_id 行（视频目录模式 video_id 为空，跳过）
    if task["status"] == "draft":
        for it in d.query_all(
            "SELECT video_id FROM publish_task_item "
            "WHERE task_id=? AND video_id IS NOT NULL AND video_id != ''",
            (task_id,),
        ):
            d.execute("UPDATE generated_video SET status='idle' WHERE id=?", (it["video_id"],))
    d.soft_delete_by_id("publish_task", task_id)
    d.execute("DELETE FROM publish_task_item WHERE task_id=?", (task_id,))


# ---------- 调度执行（F-05.3） ----------

def _dispatch_items(item_ids: list[str]) -> int:
    """#455 改：按 task_id 分组 submit 投递——每 publish_task 一次 submit。

    旧版每 item submit → publish 池任务数 = 明细数（一个视频一个任务队列）。
    新版按 task_id 分组 → 每 task 一次 submit，worker 函数 _publish_one_task 内部
    循环原子 UPDATE waiting→publishing 抢占并调 inner（task_service pool max_workers=1
    仍限制并发）。

    关键修复 #bug：原版 dispatch 阶段对每条 item 都原子 UPDATE waiting→publishing，结果
    所有 waiting 都被抢光（但只 submit 1 次），worker 进入 while 时 SELECT waiting 已空
    → 立刻 break，task 永远卡在「全 publishing / 不前进」。修：dispatch 只 submit 不抢 item，
    由 worker 自己循环抢占。

    参数:
        item_ids: 已在外层过滤好的 publish_task_item.id 列表
    返回:
        实际成功 submit 的 task 数（去重后）

    复用点：
    - dispatch_all_waiting：confirm/resume/启动恢复 后拉全部 waiting id 后调

    幂等：submitted_tasks 去重保证同 task 只 submit 一次（worker 已在跑 → 跳过）。
    """
    if not item_ids:
        return 0
    d = get_db()
    # #confirm-kickoff-2：dispatch 阶段原子 UPDATE waiting→publishing，让前端 confirm 后立即看到
    # 全部明细状态翻转（不等 worker 启动后再抢）。原子抢与 worker 抢占使用同一条 SQL，
    # 并发安全；worker 起来后 SELECT waiting 已空 → break，无副作用。
    marks = ",".join("?" * len(item_ids))
    d.execute(
        f"""UPDATE publish_task_item SET status='publishing'
            WHERE id IN ({marks}) AND status='waiting'""",
        tuple(item_ids))
    # 预取去重的 task_id + task_name：dispatch 不再动 item.status，只决定 submit 哪些 task
    rows = d.query_all(
        f"""SELECT DISTINCT pti.task_id, pt.task_name
            FROM publish_task_item pti
            JOIN publish_task pt ON pt.id = pti.task_id
            WHERE pti.id IN ({marks})""",
        tuple(item_ids))

    submitted_tasks: set[str] = set()
    # #455：跨线程并发时两个 worker 都可能抢到不同 item 但同 task → submitted_tasks 必须线程安全
    submit_lock = threading.Lock()
    count = 0
    for r in rows:
        task_id = r["task_id"]
        with submit_lock:
            if task_id in submitted_tasks:
                # 同 task 已 submit（worker 还在跑）→ 跳过
                continue
            try:
                task_service.submit("publish", r["task_name"],
                                    lambda info, tid=task_id: _publish_one_task(tid, info))
                submitted_tasks.add(task_id)
                count += 1
            except Exception as exc:  # noqa: BLE001
                # submit 失败：items 保持 waiting，需手动 retry/重新 confirm 再派发
                logger.error("[dispatch] submit 失败 task={} err={}", task_id[:8], exc)
                raise
    return count


def dispatch_all_waiting(task_id: str) -> int:
    """#confirm-kickoff-2：确认后派发任务下全部 waiting 明细（按 plan_time 升序）。

    一次拉全部 waiting 明细，原子 UPDATE 抢占（与其他 worker 并发安全）；
    submit 到 publish 池（max_workers=1 同账号串行 / 跨账号并行），前端立刻看到
    全部 waiting→publishing 的进度推进；
    - _publish_one_item 仍把 item.plan_time 作为 schedule 透传给抖音，平台仍按原
      时刻 release；本地只是更早把请求投递到 publish 池。

    幂等性：原子 UPDATE WHERE status='waiting' 保证同一明细只被一个 worker 抢占；
    重复调用本函数也会因 status 已变 publishing/success/failed 而返回 0。

    必须在事务外调用（事务内起 publish 线程池会持锁竞争）。
    """
    d = get_db()
    # #bugfix：暂停门——task.status != 'running' 时拒绝派发
    cur_task = d.query_one("SELECT status FROM publish_task WHERE id=? AND deleted=0", (task_id,))
    if not cur_task or cur_task["status"] != "running":
        return 0
    rows = d.query_all(
        """SELECT id FROM publish_task_item
           WHERE task_id=? AND status='waiting'
           ORDER BY plan_time ASC, id ASC""",
        (task_id,))
    ids = [r["id"] for r in rows]
    return _dispatch_items(ids)


def _publish_one_item_inner(item_id: str, info) -> str:
    """#455 改：单条 item 发布核心逻辑（被 _publish_one_task 循环调用，不直接 submit）。

    原 _publish_one_item 的完整业务：账号校验 → 标题/话题 → 上传发布 → 回查留痕。
    #418：item.source='video_dir' 时视频来自文件系统（item.video_path），不走 generated_video；
    shop_id 实际是手动输入的门店名称（poi_id 留空，发时按名称模糊匹配）。

    状态机契约：返 "success" / "partial"（账号挂起） / "failed" / "failed"（重试耗尽）。
    用户取消抛 _TaskCancelled；外层 worker 捕获后 break 循环。
    """
    d = get_db()
    item = d.query_one("SELECT * FROM publish_task_item WHERE id=?", (item_id,))
    if not item or item["status"] not in ("publishing", "waiting"):
        return "明细状态异常，跳过"
    is_video_dir = (item.get("source") or "project") == "video_dir"
    # 视频来源：video_dir 模式读 video_path 字段；项目模式查 generated_video
    # #bugfix：abs_video 必须在此阶段算好——后面 video_dir 模式构造 title 时会用到（避免 NameError）
    if is_video_dir:
        video_path = item.get("video_path") or ""
        if not video_path or not Path(video_path).exists():
            logger.warning("[发布] video_dir 明细 {} 视频文件不存在: {}",
                           item_id[:8], video_path)
            d.update_by_id("publish_task_item", item_id,
                           {"status": "failed", "fail_reason": "视频文件不存在或已被移动"})
            _refresh_task_progress(item["task_id"])
            info.message = "视频文件不存在"
            return "failed"
        # 视频目录模式：构造伪 video dict（_write_publish_record 取 file_path）
        video = {"id": "", "file_path": video_path}
        abs_video = Path(video_path)
        # 虚拟 shop 行（poi_id 留空 → publish_video 走按名称匹配路径）
        shop = {
            "id": item.get("shop_id") or "",  # 实际是 manual shop name
            "name": item.get("shop_id") or "",
            "poi_id": "",
        }
    else:
        video = d.query_one("SELECT * FROM generated_video WHERE id=?", (item["video_id"],))
        shop = d.query_one("SELECT * FROM shop WHERE id=?", (item["shop_id"],))
        abs_video = _video_abs_path(video["file_path"]) if video else None
    account = d.query_one("SELECT * FROM account WHERE id=? AND deleted=0", (item["account_id"],))

    # PR3 #56 + #59 P0 #10：进度模板 + 失败 message 字段填充
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts
    start_ts = info.start_ts or time.time()
    pub_task = d.query_one("SELECT task_name, account_ids_json, project_ids_json FROM publish_task WHERE id=?", (item["task_id"],))
    # 任务 #59 P0 #10：分子恒 1 误导——改为"项目名 + 用时"，不再秀 1/总数
    project_name = pub_task["task_name"] if pub_task else "未命名任务"
    # #v42：inner 不再写 info.progress——worker 独占控制本字段
    # （保持「成功 N / 失败 M / 总 K，用时 H:MM:SS」格式贯穿任务队列展示）

    # 账号登录态二次校验（F-05-R6）
    if not account or account["status"] in ("invalid", "disabled"):
        d.update_by_id("publish_task_item", item_id,
                       {"status": "suspended", "fail_reason": "账号登录态失效，挂起"})
        notifier.notify("warn", "publish", "发布明细挂起：账号失效",
                        f"明细 {item_id[:8]} 已挂起，账号恢复后自动继续",
                        action=f"relogin:{item['account_id']}")
        info.message = f"账号登录已失效：{account.get('nickname') or account.get('remark') or '账号不存在'}"
        # 任务 #59 P0 #2：返 "partial" 而非 "账号失效挂起"（状态机契约对齐）
        return "partial"

    # 标题/话题随机（项目池+全局池，变量替换）
    title = creation_service.random_text("title", item["project_id"]) or (video["file_path"].split("/")[-1])
    topic = creation_service.random_text("topic", item["project_id"]) or ""
    title = _replace_placeholders(title, shop)
    # #292：视频目录模式标题用 intro_snapshot + 文件名前缀；video_title = 目录名
    video_title = ""
    proj_row = None
    if not is_video_dir:
        # #94：原嵌套 f-string 假设 project 存在，无 null check；项目被删时 KeyError
        # 抛到外层 except Exception 才打 logger.warning，但没标哪个缺失。先查再 null check。
        proj_row = d.query_one("SELECT title FROM project WHERE id=?", (item["project_id"],))
        if not proj_row:
            logger.warning("[发布] project 缺失 item={} project_id={}", item_id[:8], item["project_id"])
            d.update_by_id("publish_task_item", item_id,
                           {"status": "failed", "fail_reason": "项目不存在"})
            _refresh_task_progress(item["task_id"])
            info.message = "项目不存在"
            return "failed"
        video_title = proj_row["title"]
        # 项目模式下：标题优先 intro 内容
        if item.get("intro_snapshot"):
            title = item["intro_snapshot"]
    else:
        # 视频目录模式：video_title = 目录名（从 item.project_id 查 publish_task）
        task_row = d.query_one("SELECT video_dirs_json, task_name FROM publish_task WHERE id=?", (item["task_id"],))
        try:
            vds = json.loads(task_row["video_dirs_json"] or "[]") if task_row else []
            vd = next((v for v in vds if v.get("id") == item["project_id"]), None)
            video_title = vd["dir_name"] if vd else "video_dir"
        except Exception:
            video_title = "video_dir"
        # 视频目录模式：标题用 intro 内容（manual_intros 轮转）
        if item.get("intro_snapshot"):
            title = item["intro_snapshot"]
        else:
            title = f"{video_title}_{Path(str(abs_video)).stem}"

    client = get_douyin_client()
    cookie = account_service.get_cookie(account["id"])
    logger.info("[发布] 开始发布明细 {id}：账号={acc} 门店={shop} 视频={video} source={src}",
                id=item_id[:8], acc=account["nickname"] or account["remark"],
                shop=shop["name"] if shop else "?",
                video=str(abs_video).split("/")[-1], src=item.get("source") or "project")
    retry = 0
    # 透传 plan_time 给抖音做平台定时发布；retry_task 已过滤掉过期明细，这里不再降级
    schedule_param = item.get("plan_time") or ""
    # #bugfix：plan_time 已过期 → 直接标 failed 跳过（抖音会判「定时发布时间已过」拒收，省一次重试）
    if schedule_param:
        from datetime import datetime as _dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                when = _dt.strptime(schedule_param, fmt)
                if when <= _dt.now():
                    d.update_by_id("publish_task_item", item_id,
                                   {"status": "failed", "fail_reason": "计划发布时间已过，跳过"})
                    _refresh_task_progress(item["task_id"])
                    info.message = "计划发布时间已过"
                    logger.warning("[发布] 明细 {} plan_time={} 已过期，跳过派发",
                                   item_id[:8], schedule_param)
                    return "failed"
                break
            except ValueError:
                continue
    # #129：发布循环顶部检查取消——发布动作可能要等几分钟上传/审核
    raise_for_cancel(info)
    # #高危-2：retry 期间取消 → 视频占用必须释放，否则卡在 occupied 永不退出
    try:
        # 任务 #59 P1 #8：while retry < MAX_RETRY（首次尝试 + (MAX_RETRY-1) 次重试 = MAX_RETRY 次发送）
        while retry < MAX_RETRY:
            try:
                # #bugfix：location 必须用门店**名称**而非 poi_id（抖音搜索按名称匹配；
                # poi_id 是抖音内部 ID，搜索不到会直接 fail）。视频目录模式同理。
                # shop 可能为 None（项目模式下 shop 被软删）—— 退化为空串
                shop_name = (shop.get("name") if shop else "") or ""
                result = client.publish_video(
                    cookie, str(abs_video), title, topic, shop_name,
                    account_id=account["id"],
                    schedule=schedule_param,
                    allow_save=bool(item.get("allow_download")),
                )
                if result.get("success"):
                    # #bugfix：视频目录模式先 move 到 _published/，再写 record 拿新路径
                    # 否则 record.video_local_path 存的是旧路径，「定位」按钮失效
                    if is_video_dir:
                        from app.services.video_dir_service import move_to_published
                        new_abs = move_to_published(str(abs_video))
                        video = {"id": "", "file_path": new_abs}
                    record_id = _write_publish_record(item, account, shop, title, topic,
                                                      video, video_title, result["online_video_id"])
                    d.update_by_id("publish_task_item", item_id, {
                        "status": "success", "publish_record_id": record_id, "fail_reason": None})
                    _refresh_task_progress(item["task_id"])
                    # PR3 #56 + #59 P0 #10：progress 改"项目名 + 用时"（账号/视频数分子恒 1 误导）
                    # #v42：success 走 info.message——worker 独占控制 progress 字段
                    info.message = f"发布成功：{video_title}（用时 {_fmt_hms(time.time() - start_ts)}）"
                    logger.info("[发布] 明细 {id} 发布成功，线上视频 {oid}",
                                id=item_id[:8], oid=result["online_video_id"])
                    return "success"
                # 发布明确失败：内容违规类不重试（F-05-R4）
                d.update_by_id("publish_task_item", item_id, {
                    "status": "failed", "fail_reason": result.get("message", "发布失败")})
                if not is_video_dir:
                    _release_video(item["video_id"])
                _refresh_task_progress(item["task_id"])
                info.message = result.get("message", "发布失败")
                logger.warning("[发布] 明细 {id} 明确失败：{msg}", id=item_id[:8],
                               msg=result.get("message", "发布失败"))
                # 任务 #59 P0 #2：返 "failed" 而非 "发布失败：..."（状态机契约对齐）
                return "failed"
            except LoginInvalidError:
                d.update_by_id("publish_task_item", item_id,
                               {"status": "suspended", "fail_reason": "账号登录态失效，挂起"})
                d.update_by_id("account", account["id"], {"status": "invalid"})
                notifier.notify("warn", "publish", "发布失败：账号失效",
                                "明细已挂起", action=f"relogin:{account['id']}")
                info.message = f"账号登录已失效：{account.get('nickname') or account.get('remark') or account['id']}"
                logger.warning("[发布] 明细 {id} 账号失效挂起", id=item_id[:8])
                # 任务 #59 P0 #2：账号失效挂起视为"部分失败"（业务上后续会重试）
                return "partial"
            except RiskControlError as e:
                retry += 1
                logger.warning("[发布] 明细 {id} 风控拦截（第 {r}/{m} 次重试）：{e}",
                               id=item_id[:8], r=retry, m=MAX_RETRY, e=e)
                if retry >= MAX_RETRY:
                    d.update_by_id("publish_task_item", item_id,
                                   {"status": "failed", "fail_reason": f"风控拦截：{e}"})
                    if not is_video_dir:
                        _release_video(item["video_id"])
                    _refresh_task_progress(item["task_id"])
                    info.message = "抖音风控拦截，稍后自动重试"
                    # 任务 #59 P0 #2：返 "failed" 状态机契约对齐
                    return "failed"
                interruptible_sleep(min(60 * retry, 300), info)
            except Exception as e:  # noqa: BLE001 网络类可重试
                retry += 1
                logger.warning("[发布] 明细 {id} 异常（第 {r}/{m} 次重试）：{e}",
                               id=item_id[:8], r=retry, m=MAX_RETRY, e=e)
                if retry >= MAX_RETRY:
                    d.update_by_id("publish_task_item", item_id,
                                   {"status": "failed", "fail_reason": str(e)})
                    if not is_video_dir:
                        _release_video(item["video_id"])
                    _refresh_task_progress(item["task_id"])
                    info.message = f"发布异常：{e}"
                    # 任务 #59 P0 #2：返 "failed" 状态机契约对齐
                    return "failed"
                interruptible_sleep(min(60 * retry, 300), info)
        info.message = "多次重试仍失败"
        # 任务 #59 P0 #2：返 "failed" 状态机契约对齐
        return "failed"
    except _TaskCancelled:
        # #高危-2：取消路径释放视频 + 标 cancelled（避免 retry 期间 cancel 视频卡死）
        # 已成功发布的（status=success）跳过——上面 return 已成功路径
        current = d.query_one("SELECT status FROM publish_task_item WHERE id=?", (item_id,))
        if current and current["status"] not in ("success", "failed", "cancelled"):
            d.update_by_id("publish_task_item", item_id, {"status": "cancelled", "fail_reason": "用户取消"})
            if not is_video_dir:
                _release_video(item["video_id"])
            _refresh_task_progress(item["task_id"])
        info.message = "已取消"
        logger.info("[发布] 明细 {id} 用户取消，视频已释放", id=item_id[:8])
        raise


def _publish_one_task(task_id: str, info) -> str:
    """#455：task 维度 worker——按 plan_time 顺序逐条执行 item 直到全部 done 或取消。

    旧版 _publish_one_item 每个 publish_task_item 一次 submit → publish 池任务数 = 明细数。
    新版：每 publish_task 一次 submit，worker 内循环原子抢占 waiting item → 调 _publish_one_item_inner。
    publish 池 max_workers=1 限制不变（跨 task 串行），UI 进度按 task 维度展示。

    进度模板："{task_name}（成功 N / 失败 M / 总 K，用时 X）"

    取消语义：用户取消 → _publish_one_item_inner 抛 _TaskCancelled → 外层 break 循环 + return "partial"
    （task_service 把 info.status 设为 partial；DB 中 remaining waiting items 由 cancel_task API
    后续批量转 cancelled，worker 不再触碰）。
    """
    import time as _time
    d = get_db()
    start_ts = info.start_ts or _time.time()
    task = d.query_one("SELECT task_name, status FROM publish_task WHERE id=?", (task_id,))
    task_name = (task["task_name"] if task else "未命名任务")
    total = d.query_one(
        "SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=?", (task_id,))["c"]

    done_count = 0
    any_partial = False
    any_failed = False

    # #v42：worker 进入即写标准 progress——确保任务队列任何时刻（含执行中）都显示
    # 「成功 N / 失败 M / 总 K，用时 H:MM:SS」格式；inner 内的 progress 写入全部移除
    # 由 worker 独占控制本字段
    info.progress = f"成功 0 / 失败 0 / 总 {total}，用时 {_fmt_hms(_time.time() - start_ts)}"

    while True:
        # 顶部取消检查：每条 item 处理完都查
        try:
            raise_for_cancel(info)
        except _TaskCancelled:
            break

        # #bugfix：暂停门——task.status != 'running' 时退出循环，等待 resume 唤醒。
        # 之前 while 循环只查 item.status='waiting'，对 publish_task.status 完全无视，
        # 导致用户点暂停后已 submit 的 worker 继续把整 task 的 waiting 跑完。
        # 用 info.cache_task_status 缓存上一次结果，避免每条 item 多一次 round-trip；
        # 每 3s 强刷一次（覆盖外部 toggle 后的状态变化，避免用户感知「暂停不灵」）。
        now_ts = _time.time()
        last_check = info.cache_task_status_last_ts if hasattr(info, "cache_task_status_last_ts") else 0.0
        cached_status = info.cache_task_status if hasattr(info, "cache_task_status") else None
        need_refresh = (now_ts - last_check) >= 3 or cached_status is None
        if need_refresh:
            cur_task = d.query_one("SELECT status FROM publish_task WHERE id=?", (task_id,))
            cached_status = cur_task["status"] if cur_task else None
            info.cache_task_status = cached_status
            info.cache_task_status_last_ts = now_ts
        if not cached_status or cached_status != "running":
            # paused / cancelled / deleted：worker 退出，dispatch_all_waiting 在
            # toggle_task(paused=False) 时会主动唤醒
            break

        # 取下一条 waiting item（plan_time 升序）
        item = d.query_one(
            """SELECT id FROM publish_task_item
               WHERE task_id=? AND status='waiting'
               ORDER BY plan_time ASC, id ASC LIMIT 1""",
            (task_id,))
        if not item:
            break  # 没有 waiting → 全部 done

        # 原子抢占：防止与其他 worker 并发
        changed = d.execute(
            "UPDATE publish_task_item SET status='publishing' WHERE id=? AND status='waiting'",
            (item["id"],))
        if not changed:
            continue  # 被其他 worker 抢走，下一轮重查

        # 跑单条 item（业务逻辑原样）
        try:
            result = _publish_one_item_inner(item["id"], info)
        except _TaskCancelled:
            any_partial = True
            break

        if result == "success":
            done_count += 1
        elif result == "partial":
            # 账号挂起——后续 resume_suspended_items 可恢复；算部分成功
            done_count += 1
            any_partial = True
        else:  # failed
            any_failed = True

        # 进度更新：task_name + 实时计数 + 用时；一次 SELECT 取三计数避免 3 次 round-trip
        counts = d.query_one(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_total,
                   SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed_total
               FROM publish_task_item WHERE task_id=?""",
            (task_id,))
        total = counts["total"] or 0
        success_total = counts["success_total"] or 0
        failed_total = counts["failed_total"] or 0
        # #v42：进度列模板改为「成功 N / 失败 M / 总 K，用时 H:MM:SS」；
        # 任务队列 tab 直接展示 info.progress，不带 task_name 包裹避免冗余
        info.progress = f"成功 {success_total} / 失败 {failed_total} / 总 {total}，用时 {_fmt_hms(_time.time() - start_ts)}"

    # 终态：全失败 → failed；有失败或挂起 → partial；全成功 → success
    # 注：partial 也算 done_count=1，所以 done_count==0 + any_partial 分支不可达；仅保留失败语义
    if done_count == 0:
        return "failed"
    if any_failed or any_partial:
        return "partial"
    return "success"


def _replace_placeholders(title: str, shop: dict | None) -> str:
    """标题变量占位符替换：{门店名} {城市} {类目}。None/非字符串统一退化为空串。"""
    if not shop:
        return title

    def _s(v) -> str:
        return v if isinstance(v, str) else (v or "")

    return (title.replace("{门店名}", _s(shop.get("name", "")))
                 .replace("{城市}", _s(shop.get("city")))
                 .replace("{类目}", _s(shop.get("category", ""))))


def _video_abs_path(rel_path: str):
    """成品相对路径转绝对。"""
    from app.services.setting_service import get_data_dir
    return get_data_dir() / rel_path


def _write_publish_record(item: dict, account: dict, shop: dict, title: str, topic: str,
                          video: dict, video_title: str, online_video_id: str) -> str:
    """写发布记录（F-06-R1/R2 快照冗余）。

    #413：把 publish_task_item 上的 intro/topics/declaration/allow_download 透传，
    发布记录永久保留发布时的配置快照。

    shop 可能为 None（项目模式下 shop 被软删）—— 退化为空字符串/None。
    """
    d = get_db()
    import json as _json
    raw_topics = item.get("topics_snapshot") or "[]"
    try:
        topics_list = _json.loads(raw_topics) if isinstance(raw_topics, str) else (raw_topics or [])
    except _json.JSONDecodeError:
        topics_list = []
    shop = shop or {}
    return d.insert("publish_record", {
        "publish_title": title,
        "publish_topics": topic,
        "publish_time": now_str(),
        "video_title": video_title,
        "account_id": account["id"],
        "account_snapshot": account.get("remark") or account.get("nickname", ""),
        "shop_id": shop.get("id") or "",
        "shop_snapshot": f"{shop.get('name', '')}|{shop.get('category', '')}",
        "project_id": item["project_id"],
        "video_id": video["id"],
        "video_local_path": video["file_path"],
        "online_video_id": online_video_id,
        "task_item_id": item["id"],
        "intro_snapshot": item.get("intro_snapshot"),
        "topics_snapshot": _json.dumps(topics_list, ensure_ascii=False),
        "declaration": item.get("declaration"),
        "allow_download": item.get("allow_download"),
    })


def _release_video(video_id: str) -> None:
    """失败释放成品占用（F-05-R2；表无 update_time 列）。"""
    get_db().execute("UPDATE generated_video SET status='idle' WHERE id=?", (video_id,))


def _refresh_task_progress(task_id: str) -> None:
    """刷新任务成功/失败计数；全部终态则完成任务并通知。"""
    d = get_db()
    total = d.query_one("SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=?", (task_id,))["c"]
    done = d.query_one(
        "SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=? AND status IN ('success','failed','cancelled')",
        (task_id,))["c"]
    success = d.query_one(
        "SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=? AND status='success'", (task_id,))["c"]
    fail = d.query_one(
        "SELECT COUNT(*) AS c FROM publish_task_item WHERE task_id=? AND status='failed'", (task_id,))["c"]
    d.update_by_id("publish_task", task_id, {"success_count": success, "fail_count": fail})
    if done >= total:
        d.update_by_id("publish_task", task_id, {"status": "finished"})
        task = d.query_one("SELECT task_name FROM publish_task WHERE id=?", (task_id,))
        notifier.notify("info", "publish", f"发布任务完成：{task['task_name']}",
                        f"成功 {success} / 失败 {fail}")


def resume_suspended_items(account_id: str) -> int:
    """账号恢复后，其挂起明细恢复待发布（F-05-R6），返回恢复数。"""
    d = get_db()
    return d.execute(
        """UPDATE publish_task_item SET status='waiting'
           WHERE account_id=? AND status='suspended'""",
        (account_id,))


def retry_task(task_id: str) -> dict:
    """任务级重试：遍历 failed 明细，按 plan_time 阈值分流。

    - plan_time - now >= RETRY_MIN_GAP_HOURS → 复活 waiting，立即 dispatch_all_waiting
      主动唤醒 worker（#bugfix：旧版只复活 waiting 不主动派发 → 复活后无明细执行）
    - plan_time - now <  RETRY_MIN_GAP_HOURS → 永久 failed 关闭，释放成品占回 idle

    plan_time 一律不改（保留用户原定时）。
    返回 {retried: N, closed: M} 便于前端 toast。
    """
    d = get_db()
    task = d.query_one("SELECT id, status FROM publish_task WHERE id=?", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] == "cancelled":
        raise ValueError("已取消任务不可重试")
    # #bugfix：finished 任务允许 retry——复活明细的同时把 task 拉回 running，
    # 否则 dispatch_all_waiting gate 一直拒，明细永远 waiting 不出库
    if task["status"] == "finished":
        d.update_by_id("publish_task", task_id, {"status": "running"})
        task["status"] = "running"
    # #bugfix：paused 任务允许 retry（复活明细等 resume 派发）但 running 任务才立即 dispatch
    _task_active = task["status"] == "running"

    failed_items = d.query_all(
        "SELECT id, plan_time, video_id, retry_count FROM publish_task_item "
        "WHERE task_id=? AND status='failed'", (task_id,))
    if not failed_items:
        return {"retried": 0, "closed": 0}

    now = datetime.now()
    retried = 0
    closed = 0
    for it in failed_items:
        gap_ok = False
        if it["plan_time"]:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    when = datetime.strptime(it["plan_time"], fmt)
                    gap_ok = (when - now) >= timedelta(hours=RETRY_MIN_GAP_HOURS)
                    break
                except ValueError:
                    continue
        if gap_ok:
            # 复活：回 waiting，重新占成品，随后 dispatch_all_waiting 主动派发
            d.update_by_id("publish_task_item", it["id"], {
                "status": "waiting", "fail_reason": None, "retry_count": 0,
            })
            d.execute("UPDATE generated_video SET status='occupied' WHERE id=?",
                      (it["video_id"],))
            retried += 1
        else:
            # 关闭：保留 failed 终态，释放成品，写明关闭原因
            reason = (f"计划时间 {it['plan_time']} 距当前不足"
                      f"{RETRY_MIN_GAP_HOURS}小时，已关闭")
            d.update_by_id("publish_task_item", it["id"], {
                "status": "failed",
                "fail_reason": reason,
                "retry_count": (it["retry_count"] or 0) + 1,
            })
            d.execute("UPDATE generated_video SET status='idle' WHERE id=?",
                      (it["video_id"],))
            closed += 1

    # #bugfix：复活后必须主动 dispatch，否则等不到 worker。
    # paused 任务不 dispatch（dispatch_all_waiting 内部 gate 会拒），等 resume 唤醒
    if retried:
        logger.info("[retry_task] {} 复活 {} 条 / 关闭 {} 条", task_id, retried, closed)
        if _task_active:
            try:
                n = dispatch_all_waiting(task_id)
                if n:
                    logger.info("[retry_task] {} 主动派发 {} 条 waiting",
                                task_id[:8], n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[retry_task] dispatch 异常: {} task={}", exc, task_id[:8])
    return {"retried": retried, "closed": closed}


# ---------- 发布记录（F-06） ----------

def list_records(account_id: str = "", shop_id: str = "", project_id: str = "",
                 start_time: str = "", end_time: str = "", page: int = 1, page_size: int = 20,
                 keyword: str = "") -> dict:
    """发布记录分页查询（F-06.1；#456：单字段入参查询 + 关键词搜索）。

    参数:
        keyword: 搜索关键词，匹配 publish_title / intro_snapshot / account_snapshot / shop_snapshot
    """
    d = get_db()
    where = "WHERE 1=1"
    params: list = []
    if account_id:
        where += " AND r.account_id=?"
        params.append(account_id)
    if shop_id:
        where += " AND r.shop_id=?"
        params.append(shop_id)
    if project_id:
        where += " AND r.project_id=?"
        params.append(project_id)
    if start_time:
        where += " AND r.publish_time>=?"
        params.append(start_time)
    if end_time:
        where += " AND r.publish_time<=?"
        params.append(end_time)
    if keyword:
        # #456：关键词 4 字段模糊匹配
        where += " AND (r.publish_title LIKE ? OR r.intro_snapshot LIKE ? OR r.account_snapshot LIKE ? OR r.shop_snapshot LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    return d.query_page(
        f"""SELECT r.*, t.title AS project_title,
                   i.plan_time AS plan_time, i.status AS item_status, i.fail_reason AS item_fail_reason
            FROM publish_record r
            LEFT JOIN project t ON r.project_id=t.id
            LEFT JOIN publish_task_item i ON i.id = r.task_item_id
            {where} ORDER BY r.publish_time DESC""",
        tuple(params), page, page_size)


def export_records(filters: dict, out_path: str) -> str:
    """导出发布记录 Excel（openpyxl，F-06.3）。"""
    from openpyxl import Workbook
    result = list_records(page=1, page_size=100000, **filters)
    wb = Workbook()
    ws = wb.active
    ws.title = "发布记录"
    headers = ["发布标题", "发布话题", "发布时间", "视频标题", "账号", "门店", "本地地址", "线上作品ID"]
    ws.append(headers)
    for r in result["list"]:
        ws.append([
            r["publish_title"], r["publish_topics"], r["publish_time"], r["video_title"],
            r["account_snapshot"], r["shop_snapshot"], r["video_local_path"], r["online_video_id"],
        ])
    wb.save(out_path)
    return out_path
