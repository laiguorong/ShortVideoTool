# -*- coding: utf-8 -*-
"""创作中心服务（F-04）：项目/分镜/片段/BGM/文案池 CRUD、批量切割分配、组合生成任务。

组合规则（F-04-R10~R14）：
- 成品 = N 分镜按序各取恰 1 片段拼接；
- 组合键 = 片段 ID 有序序列（数据库唯一索引防重复）；
- 去重口径 = 组合键唯一（同一组合只生成一次）。
"""

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from app.core import video_edit, notifier


# #426/#427：画幅预设（横竖屏 + 固定尺寸）
# 用于项目新建时选择（UI 用四边形可视化，#428）
RESOLUTION_PRESETS: list[dict] = [
    {"key": "9_16",   "w": 1080, "h": 1920, "label": "9:16 竖屏", "platform": "抖音/快手"},
    {"key": "16_9",   "w": 1920, "h": 1080, "label": "16:9 横屏", "platform": "B站/西瓜/微博/腾讯"},
    {"key": "3_4",    "w": 1080, "h": 1440, "label": "3:4 竖屏", "platform": "小红书详情"},
    {"key": "2_3",    "w": 720,  "h": 1080, "label": "2:3 竖屏", "platform": "小红书/微博"},
    {"key": "1_1",    "w": 1080, "h": 1080, "label": "1:1 方形", "platform": "小红书/朋友圈"},
    {"key": "6_7",    "w": 1080, "h": 1260, "label": "6:7 竖屏", "platform": "朋友圈"},
]


def _orientation_filter(width: int, height: int) -> str | None:
    """根据项目画幅推算应过滤的素材 orientation（#429）。
    - 竖屏（h > w）：仅 portrait
    - 横屏（w > h）：仅 landscape
    - 方形（w == h）：不过滤（None）
    """
    if width == height:
        return None
    if height > width:
        return "vertical"
    return "horizontal"
from app.core.ffmpeg import extract_frame
from app.db import get_db
from app.db.utils import new_id, now_str
from app.services import material_service
from app.services.setting_service import get_data_dir
from app.services.task_service import task_service, raise_for_cancel, _TaskCancelled, _fmt_hms, TaskInfo

# 上限（F-04-R1）
MAX_SHOTS = 50
MAX_CLIPS_PER_SHOT = 200
# 项目一生累计能生成的最大视频数（生成上限；与单次生成量、product 迭代上限无关）
MAX_COMBINATIONS = 10000  # #414：可生成数据默认最大为 10000


# ---------- 项目 CRUD ----------

def create_project(title: str, width: int = 1080, height: int = 1920,
                   fit_mode: str = "cover") -> dict:
    """创建项目（自定义标题 + 画幅 + 模式）。

    参数:
        width / height: 项目画幅（#427），默认 1080x1920 = 9:16 竖屏（创建后不可改）
        fit_mode: 画面层显模式（cover/contain/fill/blur_bg），默认 cover（项目内可改）
    """
    d = get_db()
    # #99：新建项目默认音量 100%（显式覆盖表 default 0.3）
    # #427：新建项目存规范化 width/height
    if fit_mode not in ("cover", "contain", "fill", "blur_bg"):
        raise ValueError("fit_mode 非法")
    project_id = d.insert("project", {
        "title": title, "status": "normal",
        "bgm_volume": 1.0,
        "width": width, "height": height,
        "fit_mode": fit_mode,
        "output_resolution": f"{width}x{height}",  # 兼容旧字段
    })
    return get_project(project_id)


def get_project(project_id: str) -> dict:
    """项目详情（含分镜/片段数统计）。"""
    d = get_db()
    row = d.query_one("SELECT * FROM project WHERE id=? AND deleted=0", (project_id,))
    if not row:
        raise ValueError("项目不存在")
    row["shots"] = list_shots(project_id)
    row["bgm_count"] = d.query_one(
        "SELECT COUNT(*) AS c FROM project_bgm WHERE project_id=?", (project_id,))["c"]
    # #413：已生成数 = 累计生成数（项目表字段，删除成品不减）
    row["generated_total"] = int(row.get("generated_total") or 0)
    # 未占用数仍走实时 COUNT（语义是"现在还有多少没被发布的"，会随删除变化）
    idle_stats = d.query_one(
        "SELECT SUM(CASE WHEN status='idle' THEN 1 ELSE 0 END) AS idle "
        "FROM generated_video WHERE project_id=?", (project_id,))
    row["generated_idle"] = (idle_stats["idle"] or 0) if idle_stats else 0
    # 可生成视频数量（#64）：按分镜与组合规则算出的合法组合数 − 已生成
    comb = combination_available(project_id)
    row["combination_legal_total"] = comb["legal_total"]
    row["combination_available"] = comb["available"]
    return row


def list_projects(page: int = 1, page_size: int = 20) -> dict:
    """项目列表（含分镜数/片段数/组合上界/已生成数）。

    返回每行的统计字段:
        shot_count_real: 分镜数
        clip_count: 全部分镜的片段总数（#77）
        generated_total: 已生成成品条数
        generated_idle: 已生成成品中未被发布占用的条数
        combination_total: 可生成数（合法组合上界，倒推规则，有空分镜则为 0）
    """
    d = get_db()
    rows = d.query_page(
        """SELECT t.*,
                  (SELECT COUNT(*) FROM project_shot s WHERE s.project_id=t.id) AS shot_count_real,
                  (SELECT COUNT(*) FROM project_shot_clip c
                     JOIN project_shot s ON c.shot_id=s.id
                    WHERE s.project_id=t.id) AS clip_count,
                  -- #413：累计生成数走 project.generated_total；已生成数=总量，generated_idle 仍实时
                  t.generated_total AS generated_total,
                  (SELECT COUNT(*) FROM generated_video v WHERE v.project_id=t.id AND v.status='idle') AS generated_idle
           FROM project t WHERE t.deleted=0 ORDER BY t.create_time DESC""",
        (), page, page_size)
    # 计算可生成数（倒推规则；#64）
    for r in rows["list"]:
        r["combination_total"] = _legal_combination_total(r["id"])
    return rows


def update_project(project_id: str, title: str | None, bgm_strategy: str | None,
                    dedup_rules: dict | None, output_resolution: str | None,
                    keep_original_audio: bool | None = None,
                    bgm_volume: float | None = None,
                    fit_mode: str | None = None,
                    width: int | None = None,  # 兼容旧 API；创建后锁定
                    height: int | None = None) -> dict:
    """更新项目配置。

    参数:
        keep_original_audio: #96 项目级"保留原视频声音"开关
        bgm_volume: 项目级 BGM 音量比例（0.05~1.0，None=不变），生成时统一应用
        fit_mode: #多画面适配 cover/contain/fill/blur_bg（项目内可改）
        width / height / output_resolution: #427 项目画幅；创建后锁定，update_project 忽略
    """
    fields: dict = {}
    if title is not None:
        fields["title"] = title
    if bgm_strategy is not None:
        if bgm_strategy not in ("order", "random"):
            raise ValueError("bgm_strategy 非法")
        fields["bgm_strategy"] = bgm_strategy
    if dedup_rules is not None:
        fields["dedup_rules_json"] = json.dumps(dedup_rules, ensure_ascii=False)
    # #427：output_resolution 创建后锁定（与 width/height 同步），update_project 忽略
    if output_resolution is not None:
        logger.warning("[update_project] output_resolution={} 已锁定，创建后不可改；忽略",
                       output_resolution)
    if keep_original_audio is not None:
        # #96：项目级"保留原视频声音"开关（0/1 存 SQLite）
        fields["keep_original_audio"] = 1 if keep_original_audio else 0
    if bgm_volume is not None:
        # 项目级 BGM 音量比例（0.05~1.0），生成时统一应用到 render_video_full
        if not (0.05 <= bgm_volume <= 1.0):
            raise ValueError("bgm_volume 必须在 0.05~1.0 之间")
        fields["bgm_volume"] = bgm_volume
    if fit_mode is not None:
        # #多画面适配（项目内可改）
        if fit_mode not in ("cover", "contain", "fill", "blur_bg"):
            raise ValueError("fit_mode 非法")
        fields["fit_mode"] = fit_mode
    # #427：width/height 创建后锁定，update_project 不接受修改（参数保留兼容）
    if width is not None or height is not None:
        logger.warning("[update_project] width/height 创建后锁定，忽略修改请求")
    get_db().update_by_id("project", project_id, fields)
    return get_project(project_id)


def delete_project(project_id: str, delete_files: bool) -> None:
    """删除项目（F-04-R8：二次确认+文件处置选择；#363 路径按 project_id 聚合）。

    项目内片段/封面目录 `project/<project_id>/` 是项目私有中间产物，一律清理；
    delete_files 仅管 finished/ 成品（按用户选择）。
    """
    d = get_db()
    if not d.query_one("SELECT id FROM project WHERE id=? AND deleted=0", (project_id,)):
        raise ValueError("项目不存在")
    if delete_files:
        finished_dir = get_data_dir() / "finished" / project_id
        if finished_dir.exists():
            shutil.rmtree(finished_dir, ignore_errors=True)
    # #363：项目内 clip/cover 都在 project/<project_id>/ 下，一次性清理（中间产物一律清）
    project_dir = get_data_dir() / "project" / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)
    d.soft_delete_by_id("project", project_id)


def copy_project(project_id: str, new_title: str) -> dict:
    """复制项目（分镜/片段/BGM/文案池全拷贝，游标重置）。"""
    d = get_db()
    src = get_project(project_id)
    new_id_ = d.insert("project", {
        "title": new_title,
        "bgm_strategy": src["bgm_strategy"],
        "dedup_rules_json": src["dedup_rules_json"],
        "output_resolution": src["output_resolution"],
        "status": "normal",
    })
    # 分镜+片段拷贝（片段文件按新 ID 复制一份，落盘独立；#363 新项目内重新分配 seq）
    need_render: list[str] = []
    for shot in list_shots(project_id):
        new_shot_id = d.insert("project_shot", {
            "project_id": new_id_, "name": shot["name"], "sort_order": shot["sort_order"]})
        for clip in shot["clips"]:
            new_seq = _next_clip_seq(new_id_)
            new_clip_id = new_id()
            d.insert("project_shot_clip", {
                "id": new_clip_id,
                "shot_id": new_shot_id, "material_id": clip["material_id"],
                "clip_start_ms": clip["clip_start_ms"], "clip_end_ms": clip["clip_end_ms"],
                "mirrored": clip["mirrored"], "sort_order": clip["sort_order"],
                "seq": new_seq,
                # v28：按新 project_id + seq 预写预期 file_path
                "file_path": _clip_rel_path(new_id_, new_seq, new_clip_id),
                "file_status": "pending",
                "fail_reason": None, "thumb_path": None,
                # #423：复制时继承 source_index（避免算法约束 1 把新项目全归同组）
                "source_index": clip.get("source_index")})
            # 复制已渲染的片段视频与缩略图到新路径（#363：按新 project_id+seq 落盘）
            new_file_rel = _copy_asset(clip.get("file_path"), _clip_rel_path(new_id_, new_seq, new_clip_id))
            new_thumb_rel = _copy_asset(clip.get("thumb_path"), _clip_thumb_rel(new_id_, new_seq, new_clip_id))
            if new_file_rel:
                d.update_by_id("project_shot_clip", new_clip_id, {
                    "file_path": new_file_rel, "file_status": "ready",
                    "thumb_path": new_thumb_rel})
            else:
                need_render.append(new_clip_id)
    if need_render:
        _spawn_clip_render(need_render)
    # BGM 拷贝
    for bgm in d.query_all("SELECT * FROM project_bgm WHERE project_id=?", (project_id,)):
        d.insert("project_bgm", {"project_id": new_id_, "material_id": bgm["material_id"],
                                  "sort_order": bgm["sort_order"]})
    return get_project(new_id_)


# ---------- 分镜管理（F-04.3） ----------

def list_shots(project_id: str) -> list[dict]:
    """分镜列表（含各自片段），按 sort_order。"""
    d = get_db()
    shots = d.query_all(
        "SELECT * FROM project_shot WHERE project_id=? ORDER BY sort_order",
        (project_id,))
    for s in shots:
        s["clips"] = d.query_all(
            """SELECT c.*, m.title AS material_title, m.file_path AS material_path,
                      m.file_status AS material_status, m.orientation,
                      m.cover_url AS material_cover_url,
                      m.duration_ms AS material_duration_ms
               FROM project_shot_clip c LEFT JOIN material m ON c.material_id=m.id
               WHERE c.shot_id=? ORDER BY c.sort_order, c.create_time""",
            (s["id"],))
        s["clip_count"] = len(s["clips"])
        s["clip_ready_count"] = sum(1 for c in s["clips"] if c.get("file_status") == "ready")
        s["clip_pending_count"] = sum(1 for c in s["clips"] if c.get("file_status") == "pending")
    return shots


def clips_status(project_id: str, since_rev: int = 0) -> dict:
    """片段状态差异轮询（#轮询优化）：返 rev > since_rev 的 clip，payload 100x 缩减。

    参数:
        project_id: 项目 ID
        since_rev: 上次响应的 max_rev，0 = 全量（首次调用）。
    返回:
        {
          "clips": [{id, shot_id, file_path, thumb_path, file_status, fail_reason, rev}, ...],
          "max_rev": int,   # 本次最大 rev，供下次 since_rev 用
          "pending": int,   # 当前项目 pending 数（前端决定是否继续轮询）
          "failed": int,    # 当前项目 failed 数（前端可提示"X 个失败"）
        }
    """
    d = get_db()
    # #386 状态卡住修复：since_rev <=0（首次/重置）时拉项目内所有 pending/failed，
    # 否则这些存量 rev=0 的 clip 永远拿不到（与 since_rev=0 等价命中），前端转圈卡死。
    # 后续轮询仍走 rev > since_rev 差分；max_rev 一并取项目内全局最大，避免回退。
    if since_rev <= 0:
        rows = d.query_all(
            """SELECT c.id, c.shot_id, c.file_path, c.thumb_path, c.file_status,
                      c.fail_reason, c.rev
               FROM project_shot_clip c JOIN project_shot s ON c.shot_id=s.id
               WHERE s.project_id=? AND c.file_status IN ('pending','failed')""",
            (project_id,))
        # 全局 max_rev（含已 ready 的），保证下一次 since_rev 能正确跳到最新差分点
        max_rev_row = d.query_one(
            """SELECT COALESCE(MAX(c.rev),0) AS m FROM project_shot_clip c
               JOIN project_shot s ON c.shot_id=s.id WHERE s.project_id=?""",
            (project_id,))
        max_rev = int(max_rev_row["m"] or 0) if max_rev_row else 0
    else:
        rows = d.query_all(
            """SELECT c.id, c.shot_id, c.file_path, c.thumb_path, c.file_status,
                      c.fail_reason, c.rev
               FROM project_shot_clip c JOIN project_shot s ON c.shot_id=s.id
               WHERE s.project_id=? AND COALESCE(c.rev, 0) > ?""",
            (project_id, since_rev))
        # 强制 int（SQLite 拿回 rev 可能是 None）
        max_rev = max(((int(r["rev"] or 0)) for r in rows), default=since_rev)
    # 项目级 pending/failed 统计（前端停止条件）
    cnt = d.query_one(
        """SELECT
              SUM(CASE WHEN c.file_status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN c.file_status='failed'  THEN 1 ELSE 0 END) AS failed
           FROM project_shot_clip c JOIN project_shot s ON c.shot_id=s.id
           WHERE s.project_id=?""",
        (project_id,))
    pending_count = int(cnt["pending"] or 0)
    return {
        "clips": rows,
        "max_rev": max_rev,
        "pending": pending_count,
        "failed": int(cnt["failed"] or 0),
        # #408：项目级"分切处理中"状态（pending 数 > 0 即视为正在分切），供前端顶部警示与轮询决策
        "cutting": pending_count > 0,
    }


def add_shot(project_id: str, name: str = "", after_shot_id: str | None = None,
             before_shot_id: str | None = None) -> dict:
    """添加分镜（上限 50）。

    参数:
        after_shot_id: 在该分镜之后插入（#62）
        before_shot_id: 在该分镜之前插入（#67；选第一个分镜即插入到最前）
        两者均空则追加到末尾
    """
    d = get_db()
    if not d.query_one("SELECT id FROM project WHERE id=? AND deleted=0", (project_id,)):
        raise ValueError("项目不存在")
    count = d.query_one(
        "SELECT COUNT(*) AS c FROM project_shot WHERE project_id=?", (project_id,))["c"]
    if count >= MAX_SHOTS:
        raise ValueError(f"分镜数量上限 {MAX_SHOTS}")

    anchor_id = before_shot_id or after_shot_id
    if anchor_id:
        anchor = d.query_one(
            "SELECT * FROM project_shot WHERE id=? AND project_id=?",
            (anchor_id, project_id))
        if not anchor:
            raise ValueError("参照分镜不存在")
        # before：占用该分镜的位置，它及其后整体后移；after：插到其后
        order = anchor["sort_order"] + (1 if after_shot_id else 0)
        d.execute(
            "UPDATE project_shot SET sort_order=sort_order+1 WHERE project_id=? AND sort_order>=?",
            (project_id, order))
    else:
        order = d.query_one(
            "SELECT COALESCE(MAX(sort_order),-1)+1 AS o FROM project_shot WHERE project_id=?",
            (project_id,))["o"]

    shot_id = d.insert("project_shot", {
        "project_id": project_id, "name": name or f"分镜{count + 1}", "sort_order": order})
    d.update_by_id("project", project_id, {"shot_count": count + 1})
    _safe_refresh_project_legal_total(project_id)
    return d.query_one("SELECT * FROM project_shot WHERE id=?", (shot_id,))


def rename_shot(shot_id: str, name: str) -> None:
    """分镜重命名。"""
    get_db().update_by_id("project_shot", shot_id, {"name": name})


def move_shot(shot_id: str, direction: str) -> None:
    """分镜上移/下移（相邻交换）。"""
    d = get_db()
    shot = d.query_one("SELECT * FROM project_shot WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("分镜不存在")
    delta = -1 if direction == "up" else 1
    neighbor = d.query_one(
        "SELECT * FROM project_shot WHERE project_id=? AND sort_order=?",
        (shot["project_id"], shot["sort_order"] + delta))
    if not neighbor:
        return  # 首分镜上移/末分镜下移静默
    d.update_by_id("project_shot", shot_id, {"sort_order": neighbor["sort_order"]})
    d.update_by_id("project_shot", neighbor["id"], {"sort_order": shot["sort_order"]})


def delete_shot(shot_id: str) -> None:
    """删除分镜及其片段（同时清理片段落盘文件）。"""
    d = get_db()
    shot = d.query_one("SELECT * FROM project_shot WHERE id=?", (shot_id,))
    if not shot:
        raise ValueError("分镜不存在")
    clip_rows = d.query_all("SELECT * FROM project_shot_clip WHERE shot_id=?", (shot_id,))
    d.execute("DELETE FROM project_shot_clip WHERE shot_id=?", (shot_id,))
    d.execute("DELETE FROM project_shot WHERE id=?", (shot_id,))
    _cleanup_clip_files(clip_rows)
    # 重排剩余分镜顺序
    shots = d.query_all(
        "SELECT id FROM project_shot WHERE project_id=? ORDER BY sort_order", (shot["project_id"],))
    for i, s in enumerate(shots):
        d.update_by_id("project_shot", s["id"], {"sort_order": i})
    _safe_refresh_project_legal_total(shot["project_id"])


# ---------- 路径迁移（#363：旧 create/clip/* + cache/clip/* → project/<pid>/...） ----------

def migrate_clip_paths_to_project() -> dict:
    """#363 一次性迁移：老路径 `create/clip/<id>.mp4` 与 `cache/clip/<id>.jpg` 搬到按项目分目录。

    触发条件：DB 内 file_path 或 thumb_path 以 `create/clip/` 或 `cache/clip/` 开头
    （新数据全部以 `project/` 开头，跳过）。

    seq 分配：按 project_id 分组，每组内按 create_time 升序分配 1, 2, 3...。
    文件不存在时只更新 DB 路径（与现有"file 缺失"语义一致）。

    返回: {"moved_files": 数量, "updated_rows": 数量}
    """
    d = get_db()
    data_dir = get_data_dir()
    rows = d.query_all("""
        SELECT c.id AS clip_id, c.file_path, c.thumb_path, c.create_time, c.seq,
               s.project_id AS project_id
        FROM project_shot_clip c
        JOIN project_shot s ON c.shot_id = s.id
        WHERE c.file_path LIKE 'create/clip/%' OR c.thumb_path LIKE 'cache/clip/%'
    """)
    if not rows:
        return {"moved_files": 0, "updated_rows": 0}

    # 按 project 分组，按 create_time 排序后分配 seq（缺 seq 的行才参与排序分配）
    by_project: dict[str, list[dict]] = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append(r)
    seq_assign: dict[str, int] = {}  # clip_id -> 新 seq
    for pid, items in by_project.items():
        items.sort(key=lambda x: (x["create_time"] or "", x["clip_id"]))
        for i, it in enumerate(items, start=1):
            seq_assign[it["clip_id"]] = i

    moved = 0
    updated = 0
    for r in rows:
        cid = r["clip_id"]
        pid = r["project_id"]
        seq = seq_assign[cid]
        new_file = _clip_rel_path(pid, seq, cid)
        new_thumb = _clip_thumb_rel(pid, seq, cid)
        if r["file_path"] and r["file_path"] != new_file:
            src = data_dir / r["file_path"]
            if src.is_file():
                dst = data_dir / new_file
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                moved += 1
        if r["thumb_path"] and r["thumb_path"] != new_thumb:
            src = data_dir / r["thumb_path"]
            if src.is_file():
                dst = data_dir / new_thumb
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                moved += 1
        d.update_by_id("project_shot_clip", cid, {
            "file_path": new_file,
            "thumb_path": new_thumb,
            "seq": seq,
        })
        updated += 1
    return {"moved_files": moved, "updated_rows": updated}


# ---------- 片段管理（F-04.4 / F-04.5 / #56） ----------

def _check_clip_limit(shot_id: str, adding: int) -> None:
    """分镜片段数量上限校验。"""
    d = get_db()
    current = d.query_one("SELECT COUNT(*) AS c FROM project_shot_clip WHERE shot_id=?", (shot_id,))["c"]
    if current + adding > MAX_CLIPS_PER_SHOT:
        raise ValueError(f"单分镜片段上限 {MAX_CLIPS_PER_SHOT}")


def _split_material_segments(material: dict, cut_mode: str, fixed_seconds: float,
                             trim_head_s: float, trim_tail_s: float,
                             min_clip_seconds: float) -> list[tuple[int, int]]:
    """将单个视频素材按切割方式切分为片段列表 [(起始ms, 结束ms)...]。

    参数:
        material: 素材记录
        cut_mode: whole=整条 / fixed=固定时长 / scene=按场景（近似为固定时长）/ trim_after=去头尾后固定
        fixed_seconds: 固定切割秒数
        trim_head_s / trim_tail_s: 去头尾秒数（cut_mode=trim_after 时先去再切）
        min_clip_seconds: 最短片段过滤（小于则丢弃该段）
    返回:
        片段起止毫秒列表；素材不可用返回空列表（调用方计入 skipped）
    """
    if not material or material["type"] != "video":
        return []
    if material["file_status"] != "normal":
        return []
    duration_ms = material["duration_ms"] or 0
    if duration_ms <= 0:
        duration_ms = video_edit.probe_duration_ms(str(material_service.full_path_of(material)))
    if duration_ms <= 0:
        return []
    head_ms = int(trim_head_s * 1000) if cut_mode == "trim_after" else 0
    tail_ms = int(trim_tail_s * 1000) if cut_mode == "trim_after" else 0
    effective = duration_ms - head_ms - tail_ms
    if effective < min_clip_seconds * 1000:
        return []
    segs: list[tuple[int, int]] = []
    if cut_mode == "whole":
        segs.append((head_ms, duration_ms - tail_ms))
    else:
        # fixed / scene（近似）/ trim_after+fixed
        step = int((fixed_seconds or 10) * 1000)
        pos = head_ms
        while pos + min_clip_seconds * 1000 <= duration_ms - tail_ms:
            end = min(pos + step, duration_ms - tail_ms)
            segs.append((pos, end))
            pos = end
    return segs


def add_clips_from_materials(project_id: str, material_ids: list[str],
                             cut_mode: str = "whole",
                             fixed_seconds: float = 0, trim_head_s: float = 0, trim_tail_s: float = 0,
                             min_clip_seconds: float = 3,
                             target_shot_ids: list[str] | None = None) -> dict:
    """批量从素材库添加视频到分镜（#56 / #85）。

    分配规则:
        - 指定了 target_shot_ids（#85）：所选素材的全部片段都添加到每一个目标分镜。
          「不切割」下每个素材只有 1 段，即每个目标分镜都拿到全部所选素材；
          未落在本项目内的分镜 ID 会被忽略，忽略后为空则退化为下面的自动分配。
        - 分镜列表为空：以第 1 个视频的分割段数为分镜数自动创建分镜，
          该视频各段按序分入各分镜（一段一分镜）。
        - 分镜列表非空：每个视频的分割片段按序从分镜 1 分配（一段一分镜），
          超出分镜数的片段丢弃；分镜跨视频累积多个候选片段。
    参数:
        project_id: 目标项目（空分镜时按首视频段数自动建分镜）
        material_ids: 源视频素材列表
        cut_mode / fixed_seconds / trim_head_s / trim_tail_s / min_clip_seconds: 切割配置
        target_shot_ids: 显式目标分镜列表（#85）；为空时按分镜序自动分配（#56）
    返回:
        {"clip_count": 新增片段数, "skipped": 跳过数, "per_shot": {shot_id: n}}
    """
    d = get_db()
    project_row = d.query_one(
        "SELECT width, height FROM project WHERE id=? AND deleted=0", (project_id,))
    if not project_row:
        raise ValueError("项目不存在")

    # #429：按项目画幅过滤素材 orientation
    # 竖屏项目（h>w）只用 vertical 素材；横屏项目（w>h）只用 horizontal 素材；
    # 方形（w==h）不过滤。
    proj_w = int(project_row.get("width") or 1080)
    proj_h = int(project_row.get("height") or 1920)
    target_orientation = _orientation_filter(proj_w, proj_h)

    # 取素材并切割（保留入参顺序）
    materials: list[dict] = []
    skipped = 0
    for mid in material_ids:
        m = material_service.get_material(mid)
        if m is None:
            skipped += 1
            continue
        # #429：orientation 不匹配则跳过（计入 skipped，便于前端提示）
        if target_orientation and m.get("orientation") and \
                m.get("orientation") != target_orientation:
            logger.info(
                "[add-clips] 跳过素材：项目画幅 {}x{}（{}），素材 orientation={}",
                proj_w, proj_h, target_orientation, m.get("orientation"))
            skipped += 1
            continue
        segs = _split_material_segments(m, cut_mode, fixed_seconds, trim_head_s,
                                        trim_tail_s, min_clip_seconds)
        if not segs:
            skipped += 1
            continue
        materials.append((m, segs))
    if not materials:
        return {"clip_count": 0, "skipped": skipped, "per_shot": {}}

    # 现有分镜（按 sort_order）
    shots = d.query_all(
        "SELECT id FROM project_shot WHERE project_id=? ORDER BY sort_order",
        (project_id,))
    shot_ids = [s["id"] for s in shots]
    shot_count = len(shot_ids)

    # 空分镜：以第 1 个视频段数建分镜
    if shot_count == 0:
        first_segs = materials[0][1]
        shot_count = len(first_segs)
        if shot_count == 0:
            return {"clip_count": 0, "skipped": skipped, "per_shot": {}}
        for i in range(shot_count):
            new_shot_id = d.insert("project_shot", {
                "project_id": project_id, "name": f"分镜{i + 1}", "sort_order": i})
            shot_ids.append(new_shot_id)
        # 更新项目分镜计数冗余字段
        d.update_by_id("project", project_id, {"shot_count": shot_count})

    # 各分镜当前 sort_order 起点（追加到末尾）
    next_order: dict[str, int] = {}
    for sid in shot_ids:
        next_order[sid] = d.query_one(
            "SELECT COALESCE(MAX(sort_order),-1)+1 AS o FROM project_shot_clip WHERE shot_id=?",
            (sid,))["o"]
    # #363：项目内片段 seq 起点（每个新 clip 自增分配）
    next_seq = _next_clip_seq(project_id)

    # 来源视频序号（#60 + #423）：从 project.last_source_index 起算，
    # 处理完按视频数累加写回。删除片段不回退，天然保证唯一不复用。
    src_index_start = int(d.query_one(
        "SELECT last_source_index FROM project WHERE id=?",
        (project_id,))["last_source_index"] or 0)

    # #386 状态卡住修复：clipsStatus 用 rev > since_rev 差分，新插入的 clip rev 默认 0
    # 与前端首次 since_rev=0 永远不命中 → 渲染中状态不刷新。
    # 解决：插入时给新 clip 分配一个大于 0 的 rev 基线（项目内 max + 1），后续 worker rev+1
    # 自然递增，前端首调即拿到所有新 clip。
    rev_base = (d.query_one(
        """SELECT COALESCE(MAX(c.rev),0) AS m FROM project_shot_clip c
           JOIN project_shot s ON c.shot_id=s.id WHERE s.project_id=?""",
        (project_id,))["m"] or 0) + 1

    counts = {sid: 0 for sid in shot_ids}
    new_clip_ids: list[str] = []
    # #386：本批内新 clip 的 rev 单调递增（基线已在函数上方取好），插入时 +1 累加
    cur_rev = rev_base
    # #85：显式指定目标分镜时，所选素材的全部片段都添加到每一个目标分镜
    # （「不切割」下每个素材只有 1 段，即每个目标分镜都拿到全部所选素材）
    explicit_targets = [sid for sid in (target_shot_ids or []) if sid in set(shot_ids)]
    # #423：本次 addClips 处理的 distinct 视频数（仅累加、用于写回 project.last_source_index）
    video_count = 0
    if explicit_targets:
        counts = {sid: 0 for sid in explicit_targets}
        # 来源序号（#60 + #423）：同一素材分配到多个分镜时共用同一序号，
        # 起始值 = project.last_source_index + 1
        src_of: dict[str, int] = {}
        for material, _ in materials:
            if material["id"] not in src_of:
                video_count += 1
                src_of[material["id"]] = src_index_start + video_count
        for sid in explicit_targets:
            for material, segs in materials:
                for start_ms, end_ms in segs:
                    _check_clip_limit(sid, 1)
                    cid = new_id()
                    d.insert("project_shot_clip", {
                        "id": cid,
                        "shot_id": sid, "material_id": material["id"],
                        "clip_start_ms": start_ms, "clip_end_ms": end_ms,
                        "mirrored": 0, "sort_order": next_order[sid], "seq": next_seq,
                        "thumb_path": None,
                        # v28：按规则预写预期 file_path（不代表文件已落盘）
                        "file_path": _clip_rel_path(project_id, next_seq, cid),
                        "file_status": "pending", "fail_reason": None,
                        "source_index": src_of[material["id"]],
                        # #386：rev 基线 + i，避 0；后续 worker 切完 rev+1 继续递增
                        "rev": cur_rev})
                    cur_rev += 1
                    next_order[sid] += 1
                    next_seq += 1
                    counts[sid] += 1
                    new_clip_ids.append(cid)
    else:
        # 未指定目标分镜：按段数从大到小均衡分配（#379）
        # 规则：
        #   1) 视频按段数从大到小排序
        #   2) 处理每个视频时，按"各分镜当前片段数（含项目已有+本轮已加）"重算 C_max
        #   3) 待补分镜 = 当前片段数 < C_max 的分镜（按 sort_order），N_low = 待补数
        #   4) k <= N_low：段 i → 待补分镜[i]（均衡补充到所有低分镜）
        #   5) k > N_low：兜底走原逻辑，段 i → 分镜[i]，超出 break 丢片
        # 各分镜当前片段数（项目已有 + 本轮已加）
        cur_counts: dict[str, int] = {}
        for sid in shot_ids:
            row = d.query_one(
                "SELECT COUNT(*) AS n FROM project_shot_clip WHERE shot_id=?",
                (sid,))
            cur_counts[sid] = int(row["n"])
        # 视频按段数从大到小排
        materials_sorted = sorted(materials, key=lambda x: len(x[1]), reverse=True)
        for material, segs in materials_sorted:
            video_count += 1
            src_index = src_index_start + video_count
            k = len(segs)
            # 重算 C_max 与待补分镜列表
            C_max = max(cur_counts.values()) if cur_counts else 0
            if C_max > 0:
                shots_to_fill = [sid for sid in shot_ids if cur_counts[sid] < C_max]
            else:
                # 全空分镜时把全部纳入待补，避免 k > N_low 全部进兜底堆前缀
                shots_to_fill = list(shot_ids)
            N_low = len(shots_to_fill)
            if k <= N_low:
                # 均衡补充：段 i → 待补分镜[i]
                for i, (start_ms, end_ms) in enumerate(segs):
                    sid = shots_to_fill[i]
                    _check_clip_limit(sid, 1)
                    cid = new_id()
                    d.insert("project_shot_clip", {
                        "id": cid,
                        "shot_id": sid, "material_id": material["id"],
                        "clip_start_ms": start_ms, "clip_end_ms": end_ms,
                        "mirrored": 0, "sort_order": next_order[sid], "seq": next_seq,
                        "thumb_path": None,
                        "file_path": _clip_rel_path(project_id, next_seq, cid),
                        "file_status": "pending", "fail_reason": None,
                        "source_index": src_index,
                        # #386：rev 基线 + i
                        "rev": cur_rev})
                    cur_rev += 1
                    next_order[sid] += 1
                    next_seq += 1
                    counts[sid] += 1
                    cur_counts[sid] += 1
                    new_clip_ids.append(cid)
            else:
                # 兜底：原逻辑，段 i → 分镜[i]，超出 break 丢片
                for i, (start_ms, end_ms) in enumerate(segs):
                    if i >= shot_count:
                        break  # 超出分镜数丢弃
                    sid = shot_ids[i]
                    _check_clip_limit(sid, 1)
                    cid = new_id()
                    d.insert("project_shot_clip", {
                        "id": cid,
                        "shot_id": sid, "material_id": material["id"],
                        "clip_start_ms": start_ms, "clip_end_ms": end_ms,
                        "mirrored": 0, "sort_order": next_order[sid], "seq": next_seq,
                        "thumb_path": None,
                        "file_path": _clip_rel_path(project_id, next_seq, cid),
                        "file_status": "pending", "fail_reason": None,
                        "source_index": src_index,
                        # #386：rev 基线 + i
                        "rev": cur_rev})
                    cur_rev += 1
                    next_order[sid] += 1
                    next_seq += 1
                    counts[sid] += 1
                    cur_counts[sid] += 1
                    new_clip_ids.append(cid)

    # 异步渲染片段文件（切割 + 抽首帧，界面显示处理中）
    bg_task_id = ""
    if new_clip_ids:
        # #425：传 project_id 给切割队列，任务名=项目名
        bg_task_id = _spawn_clip_render(new_clip_ids, project_id=project_id)
    else:
        logger.warning("[add-clips] 无新片段，跳过渲染 material_ids={} skipped={}",
                       material_ids, skipped)

    # #423：累加本次视频数到 project.last_source_index（单调递增、永不回退）
    if video_count > 0:
        d.update_by_id("project", project_id, {
            "last_source_index": src_index_start + video_count})

    _safe_refresh_project_legal_total(project_id)
    return {"clip_count": len(new_clip_ids), "skipped": skipped,
            "per_shot": counts, "render_queued": len(new_clip_ids),
            "bg_task_id": bg_task_id}


def _clip_rel_path(project_id: str, seq: int, clip_id: str) -> str:
    """片段视频相对路径（data 根；#363）：project/{project_id}/clip/{seq}_{clip_id}.mp4。"""
    return f"project/{project_id}/clip/{seq}_{clip_id}.mp4"


def _clip_thumb_rel(project_id: str, seq: int, clip_id: str) -> str:
    """片段缩略图相对路径（data 根；#363）：project/{project_id}/cover/{seq}_{clip_id}.jpg。"""
    return f"project/{project_id}/cover/{seq}_{clip_id}.jpg"


def _next_clip_seq(project_id: str) -> int:
    """取项目内下一个片段 seq（项目内 max+1；不持久存储到 project 行）。"""
    d = get_db()
    row = d.query_one(
        """SELECT COALESCE(MAX(c.seq),0) AS m FROM project_shot_clip c
           JOIN project_shot s ON c.shot_id=s.id WHERE s.project_id=?""",
        (project_id,))
    return (row["m"] or 0) + 1


def _cut_clip(clip_id: str) -> tuple[bool, str]:
    """纯分割：从原素材按起止毫秒切出片段 mp4 → 抽首帧 → 写状态。

    失败由调用方（手动 retry）触发重试，不自动重试（#380）。
    成功/失败都在函数内统一写 DB（file_path / file_status / fail_reason / rev）。

    返回:
        (是否成功, 失败原因)
    """
    d = get_db()
    clip = d.query_one("""SELECT c.*, s.project_id AS project_id FROM project_shot_clip c
                          JOIN project_shot s ON c.shot_id=s.id WHERE c.id=?""", (clip_id,))
    if not clip:
        _write_clip_failed(d, clip_id, "片段不存在")
        return False, "片段不存在"
    project_id = clip["project_id"]
    seq = int(clip["seq"] or 0) or _next_clip_seq(project_id)
    rel = _clip_rel_path(project_id, seq, clip_id)
    thumb_rel = _clip_thumb_rel(project_id, seq, clip_id)
    material = material_service.get_material(clip["material_id"])
    if not material or material["file_status"] != "normal":
        _write_clip_failed(d, clip_id, "素材缺失或不可用")
        return False, "素材缺失或不可用"
    src = material_service.full_path_of(material)
    if not src.is_file():
        _write_clip_failed(d, clip_id, "素材文件不存在")
        return False, "素材文件不存在"

    out = get_data_dir() / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f".tmp_{clip_id}.mp4"
    # 分割时不再带 hflip，镜像走 _apply_mirror（#380：拆分割和镜像）
    if not video_edit.cut_clip(str(src), clip["clip_start_ms"], clip["clip_end_ms"], str(tmp)):
        _safe_unlink(tmp)
        _write_clip_failed(d, clip_id, "片段切割失败")
        return False, "片段切割失败"

    # Windows：ffmpeg 退出后句柄延迟释放，os.replace 可能撞 WinError 32。短重试。
    if not _safe_replace(tmp, out):
        _safe_unlink(tmp)
        _write_clip_failed(d, clip_id, "片段落盘失败（文件被占用）")
        return False, "片段落盘失败（文件被占用）"
    # 自适应 seek（与素材库 _extract_local_cover 分段算法完全一致）
    clip_duration_ms = int(clip["clip_end_ms"]) - int(clip["clip_start_ms"])
    thumb_out = get_data_dir() / thumb_rel
    # 抽帧失败：清残留 + 设 failed 进入手动重试入口（与素材库 #592 区别：创作中心保留 out）
    # 抽帧失败：返回 False 让调用方按失败计数（与 _apply_mirror 抽帧失败语义一致）
    if not _retry_clip_thumb(
        out, thumb_out, thumb_rel, d, clip_id, duration_ms=clip_duration_ms,
    ):
        return False, "封面抽帧失败"
    return True, ""


def _apply_mirror(clip_id: str) -> tuple[bool, str]:
    """纯镜像：对已分割好的片段 mp4 做水平翻转 + 重抽首帧 → 写状态。

    依赖 file_status='ready' 与 file_path 存在（没分割好不能镜像）。
    镜像走片段文件本身，不再重新切原视频（#380：拆分割和镜像）。
    失败由调用方（手动 retry）触发重试，不自动重试。

    返回:
        (是否成功, 失败原因)
    """
    d = get_db()
    clip = d.query_one("""SELECT c.*, s.project_id AS project_id FROM project_shot_clip c
                          JOIN project_shot s ON c.shot_id=s.id WHERE c.id=?""", (clip_id,))
    if not clip:
        _write_clip_failed(d, clip_id, "片段不存在")
        return False, "片段不存在"
    project_id = clip["project_id"]
    seq = int(clip["seq"] or 0) or _next_clip_seq(project_id)
    rel = clip["file_path"] or _clip_rel_path(project_id, seq, clip_id)
    thumb_rel = clip["thumb_path"] or _clip_thumb_rel(project_id, seq, clip_id)
    out = get_data_dir() / rel
    if not out.is_file():
        _write_clip_failed(d, clip_id, "片段文件不存在，请先完成分割")
        return False, "片段文件不存在，请先完成分割"

    tmp = out.parent / f".tmp_{clip_id}.mp4"
    if not video_edit.mirror_clip_file(str(out), str(tmp)):
        _safe_unlink(tmp)
        _write_clip_failed(d, clip_id, "镜像处理失败")
        return False, "镜像处理失败"

    if not _safe_replace(tmp, out):
        _safe_unlink(tmp)
        _write_clip_failed(d, clip_id, "镜像落盘失败（文件被占用）")
        return False, "镜像落盘失败（文件被占用）"
    # 自适应 seek（与素材库 _extract_local_cover 分段算法完全一致）
    thumb_out = get_data_dir() / thumb_rel
    # 抽帧失败：清残留 + 设 failed 进入手动重试入口（与素材库 #592 区别：创作中心保留 out）
    # 抽帧失败：返回 False 让调用方按失败计数（与 _cut_clip 抽帧失败语义一致）
    if not _retry_clip_thumb(out, thumb_out, thumb_rel, d, clip_id):
        return False, "封面抽帧失败"
    return True, ""


def _compute_clip_thumb_seek(duration_ms: int | None) -> float:
    """抽帧 seek 计算（与素材库 _extract_local_cover 分段算法完全一致）。

    - duration < 1s → 0.0（取首帧，避免短片段 seek 越过文件长度 → 0 packets → ffmpeg -22）
    - 1s ≤ duration < 3s → 中点（duration/2，首帧可能黑场）
    - duration ≥ 3s → 1.0s
    - duration 缺失/0 → 1.0s 兜底

    创作中心片段时长由用户切分决定（可能 < 1s），素材库 _extract_local_cover 用同款算法。
    """
    if not duration_ms or duration_ms <= 0:
        return 1.0
    if duration_ms < 1000:
        return 0.0
    if duration_ms < 3000:
        return duration_ms / 2000.0
    return 1.0


def _commit_clip_thumb_result(
    d, cid: str, thumb_out: Path | None, thumb_rel: str,
    thumb_ok: bool, *, rev_increment: bool = True,
) -> None:
    """统一处理片段抽帧结果：写状态 + 清残留 + rev 自增。

    - thumb_ok=True → file_status='ready' + thumb_path
    - thumb_ok=False → 清 thumb 残留 + file_status='failed' + fail_reason='封面抽帧失败'

    与素材库 #592「抽帧失败撤销入库」区别——创作中心保留 out 视频文件，
    重试入口 _spawn_clip_retry 可复用分割产物（无需重切）。

    参数:
        d: DB 实例
        cid: 片段 ID
        thumb_out: 缩略图绝对路径（已 mkdir parent），用于失败时 unlink
        thumb_rel: 缩略图相对路径（DB 存）
        thumb_ok: 是否抽帧成功
        rev_increment: 是否刷 rev（#421 全局 MAX+1）。
            - True（默认）：首次抽帧 / 重试抽帧成功 → 刷 rev 让前端差分轮询可见
            - False：重试入口第 2 段抽帧失败 → 状态仍是 failed，无新状态差分，无需刷
              （首次失败时已刷过，避免同一失败事件连续刷 2 次）

    调用示例（注意：抽帧场景已统一走 _retry_clip_thumb，本函数仅在
    _spawn_clip_retry 第 3 段「已就绪」分支与 _retry_clip_thumb 内部使用）：
        _commit_clip_thumb_result(d, clip_id, thumb_out, thumb_rel, thumb_ok)
        # 失败时不刷 rev（避免与首次失败 rev 重复）：
        _commit_clip_thumb_result(d, cid, thumb_out, thumb_rel, False, rev_increment=False)
    """
    if not thumb_ok and thumb_out:
        _safe_unlink(thumb_out)
    d.update_by_id("project_shot_clip", cid, {
        "file_status": "ready" if thumb_ok else "failed",
        "fail_reason": None if thumb_ok else "封面抽帧失败",
        "thumb_path": thumb_rel if thumb_ok else None,
    })
    if rev_increment:
        d.execute(
            "UPDATE project_shot_clip SET rev = COALESCE("
            "(SELECT MAX(rev) FROM project_shot_clip), 0) + 1 WHERE id=?",
            (cid,))


def _retry_clip_thumb(
    out: Path, thumb_out: Path, thumb_rel: str, d, cid: str,
    *, duration_ms: int | None = None,
    rev_on_fail: bool = False, log_ok: str = "", log_fail: str = "",
) -> bool:
    """抽帧共用逻辑：seek → 抽帧 → 写状态（probe 可跳过）。

    与 `_commit_clip_thumb_result` 协作：抽帧结果统一由后者落 DB + 清理残留。

    参数:
        out: 源视频绝对路径
        thumb_out: 缩略图绝对路径（mkdir parent 后由 ffmpeg 写入）
        thumb_rel: 缩略图 DB 相对路径
        duration_ms: 直接传入时长（ms）跳过 probe；None 时 probe 推断（首次 cut 后
            适用：片段时长已知 = end - start，无需再 probe；retry 路径适用：必须 probe）
        rev_on_fail: 抽帧失败时是否刷 rev（默认 False：失败是已发生过的事件，不重复刷）
        log_ok: 成功日志模板（可空）
        log_fail: 失败日志模板（可空）
    返回:
        是否抽帧成功（True=ready / False=failed）
    """
    if duration_ms is None:
        streams = video_edit._probe_clip_streams(str(out))
        duration_ms = streams.get("duration_ms") if streams else 0
    seek_s = _compute_clip_thumb_seek(duration_ms)
    thumb_out.parent.mkdir(parents=True, exist_ok=True)
    ok = extract_frame(str(out), str(thumb_out), seek_seconds=seek_s)
    _commit_clip_thumb_result(
        d, cid, thumb_out, thumb_rel, ok, rev_increment=ok or rev_on_fail,
    )
    if ok and log_ok:
        logger.info(log_ok, cid)
    elif not ok and log_fail:
        logger.warning(log_fail, cid)
    return ok


def _safe_replace(src, dst, retries: int = 10, sleep_s: float = 0.5) -> bool:
    """Windows 上 ffmpeg 退出后文件句柄延迟释放，os.replace 撞 WinError 32 时短重试。

    默认窗口 5 秒：单段 _cut_clip / _apply_mirror 已通过 run_cmd 强制 cleanup stdio 句柄
    (#380)，正常情况 1 次就成功；保留短重试兜底极端延迟场景。
    长重试仅在 _cut_clip_batch 批量落盘时显式传入（避免阻塞主线程 2 分钟）。
    """
    for i in range(retries):
        try:
            os.replace(str(src), str(dst))
            return True
        except OSError:
            if i == retries - 1:
                return False
            time.sleep(sleep_s)
    return False


def _safe_unlink(path, retries: int = 10, sleep_s: float = 0.5) -> bool:
    """unlink 撞 WinError 32 时短重试（Windows ffmpeg 句柄延迟释放）。"""
    for i in range(retries):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if i == retries - 1:
                return False
            time.sleep(sleep_s)
    return False


def _write_clip_failed(d, clip_id: str, reason: str) -> None:
    """统一写入失败状态（#380：_cut_clip / _apply_mirror 失败时调用）。

    #77：原 except Exception: pass 完全静默，DB 写挂时调用方假设成功
    （后续 _write_clip_failed 链式失败也吞），链路无声坏。改为 logger.error
    记录 clip_id + 原 reason + 异常，便于排查 SQLite 锁等故障。
    """
    try:
        d.update_by_id("project_shot_clip", clip_id, {
            "file_status": "failed",
            "fail_reason": reason[:200],
        })
    except Exception as e:  # noqa: BLE001
        # #86：reason[:80] 太短，ffmpeg stderr 首行经常 100+ 字符，关键错误
        # （"Stream specifier :v matches no streams"）被吞。放宽到 200 与 DB 一致。
        logger.error(
            "[片段状态] 写失败状态也失败 clip={} 原 reason={} 异常={}",
            clip_id, reason[:200], e,
        )


def _spawn_clip_cut(clip_ids: list[str], project_id: str = "") -> str:
    """投入 task_service 队列批量切割片段（#385：替换原 daemon 线程，避免阻塞接口）。

    入口：add-clips 新插入的全部 clip_id；render_clip 重试走单 clip 直调 _cut_clip。
    返回:
        bg_task_id：可由前端 / cancel API 查询进度与取消。
    """
    if not clip_ids:
        return ""
    # #425：优先用调用方传的 project_id；旧调用方不传时从 clips 反查
    if not project_id:
        d = get_db()
        row = d.query_one(
            """SELECT s.project_id FROM project_shot_clip c
               JOIN project_shot s ON c.shot_id=s.id WHERE c.id=? LIMIT 1""",
            (clip_ids[0],))
        project_id = row["project_id"] if row else ""
    # #425：任务名=项目名（而非「批量切割 n=N」），便于任务队列直观定位
    d = get_db()
    proj = d.query_one("SELECT title FROM project WHERE id=?", (project_id,)) if project_id else None
    name = proj["title"] if proj else "(未知项目)"
    bg_id = task_service.submit(
        "clip_cut",
        name,
        lambda info: _run_clip_cut_batch(info, list(clip_ids)),
    )
    return bg_id


def _run_clip_cut_batch(info: TaskInfo, clip_ids: list[str]) -> str:
    """task_service worker：批量切割片段，进度写到 info.progress。

    依赖 task_service clip_cut 池 max_workers=1：同类型任务串行，
    此 worker 可独占池运行全部 mid，无需内层并发。

    每完成一个 clip 回调里调用 raise_for_cancel(info)，让 /tasks/{id}/cancel
    能在段间立即停掉后续切割；剩余未切的 clip 标记 failed('已取消')。
    返回:
        "success"  全部成功；"partial" 有失败但非取消；
        其他（cancelled/抛异常）由 task_service 外层处理。
    """
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts，避免 func 内自己 time.time()
    # 与 worker 外层时钟漂移
    start_ts = info.start_ts or time.time()
    d = get_db()
    # #422：补切割日志——项目名称/视频数（distinct material_id）/片段数
    placeholders = ",".join("?" * len(clip_ids))
    summary_rows = d.query_all(
        f"""SELECT c.id, c.material_id, c.shot_id, s.project_id AS project_id
            FROM project_shot_clip c JOIN project_shot s ON c.shot_id=s.id
            WHERE c.id IN ({placeholders})""",
        clip_ids)
    project_id = summary_rows[0]["project_id"] if summary_rows else ""
    proj = d.query_one("SELECT title FROM project WHERE id=?", (project_id,)) if project_id else None
    project_title = proj["title"] if proj else "(未知项目)"
    total = len(clip_ids)
    video_total = len({r["material_id"] for r in summary_rows})
    # #425：info.name 任务名=项目名（与 _spawn_clip_cut 保持一致，便于任务队列展示）
    info.name = project_title
    # PR3 #56：进度模板统一（中文逗号→英文逗号，加用时）
    info.progress = f"开始切割片段 0/{total}（共 {video_total} 个视频）"
    logger.info(f"[片段切割] 项目名称：{project_title}，视频数：{video_total}，片段数：{total}")
    done = {"n": 0, "ok": 0}
    # #425：追踪已完成的 distinct material_id 数（视频进度）
    done_mids: set[str] = set()

    def _on_clip_done(cid: str, ok: bool) -> None:
        done["n"] += 1
        if ok:
            done["ok"] += 1
        # 找当前 cid 对应的 material_id，累加到 done_mids
        for r in summary_rows:
            if r["id"] == cid:
                done_mids.add(r["material_id"])
                break
        info.progress = (
            f"已切片段 {done['n']}/{total}，涉及视频 {len(done_mids)}/{video_total}，"
            f"用时 {_fmt_hms(time.time() - start_ts)}"
        )
        # 段间查取消请求：有则抛 _TaskCancelled，被 task_service 收为 cancelled
        raise_for_cancel(info)

    try:
        _cut_clip_batch(clip_ids, on_clip_done=_on_clip_done,
                        project_title=project_title, video_total=video_total)
        if done["ok"] == total:
            logger.info(
                f"[片段切割] 项目名称：{project_title}，视频数：{video_total}/{video_total}，"
                f"片段数：{done['ok']}/{total}")
            return "success"
        # 部分成功：task_service 视作 success 但 message 留底，前端可看出
        info.message = f"部分成功：成功 {done['ok']}/{total}"
        logger.warning(
            f"[片段切割] 项目名称：{project_title}，视频数：{video_total}/{video_total}，"
            f"片段数：{done['ok']}/{total}")
        return "partial"
    except _TaskCancelled:
        # 剩余未切的 clip 标记 cancelled，便于前端区分"取消中"
        remaining = clip_ids[done["n"]:]
        for cid in remaining:
            try:
                _write_clip_failed(d, cid, "已取消")
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception as e:  # noqa: BLE001
        # 任务 #59 P0 #22：删 info.status = "failed"（task_service 外层会自己覆写）；
        # 仅 set info.message（status 由外层根据异常设 failed）
        # #78：补 logger.error 记录项目名/已切数/未切数/异常，task_service 外层
        # 只打 [任务失败] 笼统一句话，项目级细节全丢。
        logger.error(
            "[片段切割] 整批异常终止 project={} 已切={}/{} 异常={}",
            project_title, done["n"], total, e,
        )
        info.message = str(e)
        try:
            for cid in clip_ids:
                _write_clip_failed(d, cid, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("[add-clips] 写失败状态也失败")
        raise


def _cut_clip_batch(clip_ids: list[str],
                    on_clip_done: Optional[Callable[[str, bool], None]] = None,
                    project_title: str = "",
                    video_total: int = 0) -> None:
    """一批片段按素材分组，每组一次 ffmpeg 切完（#380 模式 B）。

    流程：
    1. 查所有 clip 的 (project_id, shot_id, material_id, start_ms, end_ms)
    2. 按 material_id 分组（保留 group 内按 sort_order 排序）
    3. 每组一次 ffmpeg 跑完：cut_clips_segment
    4. 校验 ok_flags → 移动到目标位置 → 抽帧 → 写 DB
    5. 单段组（k=1）或 cut_clips_segment 失败的组 → 降级走单段 _cut_clip

    参数:
        clip_ids: 待切割 clip ID 列表
        on_clip_done: 单 clip 落定回调 (cid, ok)，#385 队列进度上报用
        project_title: #422 过程日志用
        video_total: #422 过程日志用
    """
    if not clip_ids:
        return

    d = get_db()
    placeholders = ",".join("?" * len(clip_ids))
    rows = d.query_all(
        f"""SELECT c.id, c.shot_id, c.material_id, c.clip_start_ms, c.clip_end_ms,
                   c.seq, c.source_index, c.rev,
                   c.thumb_path, c.file_path,
                   s.project_id AS project_id
            FROM project_shot_clip c
            JOIN project_shot s ON c.shot_id=s.id
            WHERE c.id IN ({placeholders})""",
        clip_ids,
    )
    if not rows:
        return

    # 按素材分组
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["material_id"], []).append(r)
    # 组内按 sort_order 稳定排序（同一素材多次 add-clips 时 seq 已是项目内递增）
    for mid in groups:
        groups[mid].sort(key=lambda r: r["seq"])

    # #422：过程中日志——当前视频（按 material_id 字典序）+ 切割片段数
    sorted_mids = sorted(groups.keys())

    def _done(cid: str, ok: bool) -> None:
        if on_clip_done:
            try:
                on_clip_done(cid, ok)
            except _TaskCancelled:
                # 取消信号必须冒泡，让 worker 停后续切割；其余异常才吞
                raise
            except Exception:  # noqa: BLE001 回调异常不影响切割
                logger.exception("[cut_batch] on_clip_done 回调异常")

    for mid in sorted_mids:
        items = groups[mid]
        # #422：过程中日志——当前视频（取 add-clips 时分配的 source_index，#60 项目内累计递增）+ 切割片段数
        mid_idx = items[0]["source_index"]
        logger.info(
            f"[片段切割] 项目名称：{project_title}，当前视频：{mid_idx}，"
            f"切割片段数：{len(items)}")
        if len(items) == 1:
            # 单段 → 直接走 _cut_clip 路径（更简单，省一次 ffmpeg 等待）
            ok, _ = _cut_clip(items[0]["id"])
            _done(items[0]["id"], ok)
            continue

        # 重复时间戳（如显式 target 多分镜同段）走单段路径：
        # segment muxer 在 segment_times 重复时只会输出一段，且各文件时长
        # 不符合预期，逐个 _cut_clip 更稳。
        if len({(int(it["clip_start_ms"]), int(it["clip_end_ms"])) for it in items}) \
                != len(items):
            logger.warning("[cut_batch] 重复时段 mid={} k={} 走逐个 _cut_clip",
                           mid, len(items))
            for it in items:
                ok, _ = _cut_clip(it["id"])
                _done(it["id"], ok)
            continue

        # 多段：准备素材
        material = material_service.get_material(mid)
        if not material or material["file_status"] != "normal":
            for it in items:
                _write_clip_failed(d, it["id"], "素材缺失或不可用")
                _done(it["id"], False)
            continue
        src = material_service.full_path_of(material)
        if not src.is_file():
            for it in items:
                _write_clip_failed(d, it["id"], "素材文件不存在")
                _done(it["id"], False)
            continue

        # 临时输出目录（按 mid + batch uuid 隔离）
        batch_id = uuid.uuid4().hex[:8]
        out_dir = get_data_dir() / "temp" / "seg_cut" / mid / batch_id

        # segment muxer 要求累积时间戳递增（按 seq 排后已递增）
        segments = [(int(it["clip_start_ms"]), int(it["clip_end_ms"])) for it in items]
        ok_flags = video_edit.cut_clips_segment(str(src), segments, str(out_dir))

        # 若整批失败（典型：素材被截断、编码异常）→ 降级逐个 _cut_clip
        if not any(ok_flags):
            logger.warning("[cut_batch] mid={} segment muxer 整批失败，降级逐个 _cut_clip",
                           mid)
            shutil.rmtree(out_dir, ignore_errors=True)
            # #98：单段降级异常 silent → 补 WARNING 含 mid/clip/异常，并聚合降级失败计数
            degrade_fail = 0
            for it in items:
                try:
                    ok, _ = _cut_clip(it["id"])
                except Exception as e:  # noqa: BLE001
                    logger.warning("[cut_batch] 降级单段异常 mid={} clip={} 异常={}", mid, it["id"], e)
                    _write_clip_failed(d, it["id"], str(e))
                    ok = False
                    degrade_fail += 1
                _done(it["id"], ok)
            if degrade_fail:
                logger.warning(
                    "[cut_batch] 降级 mid={} 总 {} 条 clip 异常 {} 条",
                    mid, len(items), degrade_fail,
                )
            continue

        # 部分/全部成功 → 写 DB + 抽帧
        for i, (it, ok) in enumerate(zip(items, ok_flags)):
            cid = it["id"]
            project_id = it["project_id"]
            seq = int(it["seq"] or 0) or _next_clip_seq(project_id)
            # v28：file_path INSERT 时已预写，直接读
            rel = it["file_path"]
            thumb_rel = it["thumb_path"] or _clip_thumb_rel(project_id, seq, cid)

            if not ok:
                _write_clip_failed(d, cid, "片段切割失败")
                _done(cid, False)
                continue

            tmp_p = out_dir / f"clip_{i:03d}.mp4"
            final_p = get_data_dir() / rel
            final_p.parent.mkdir(parents=True, exist_ok=True)  # #380 bugfix：批量路径未建父目录
            # 批量落盘：run_cmd 已 _force_close_proc_io，正常 1 次成功；
            # 给 15s 重试窗口避免极端句柄延迟，并避免长时间占死 task_service 池
            if not _safe_replace(tmp_p, final_p, retries=30, sleep_s=0.5):
                _safe_unlink(tmp_p)
                _write_clip_failed(d, cid, "片段落盘失败（文件被占用）")
                _done(cid, False)
                continue

            # 自适应 seek（与素材库 _extract_local_cover 分段算法完全一致）
            thumb_out = get_data_dir() / thumb_rel
            # 抽帧失败：清残留 + 设 failed 进入手动重试入口（与素材库 #592 区别：创作中心保留 out）
            # 抽帧失败时不报成功（#P1：进度/计数正确性）
            thumb_ok = _retry_clip_thumb(final_p, thumb_out, thumb_rel, d, cid)
            _done(cid, thumb_ok)

        # 清理临时目录
        shutil.rmtree(out_dir, ignore_errors=True)


def _spawn_clip_mirror(clip_ids: list[str]) -> None:
    """起 daemon 线程批量镜像片段（对片段文件 hflip，#380）。

    #79：原函数裸线程 + 零日志，状态栏看不到进度（也未接 task_service）。
    补入口 INFO 启动数 + 单条失败 WARNING + DB 写挂 ERROR。
    """
    if not clip_ids:
        # #99：早返回补 debug 与入口 INFO 保持日志链路一致
        logger.debug("[片段镜像] 空 clip_ids 列表，跳过启动")
        return
    logger.info("[片段镜像] 启动后台线程 clip 数={}", len(clip_ids))

    def _run(ids: list[str]) -> None:
        ok_count = 0
        fail_count = 0
        # #88：循环外取一次 db 实例，100 clip 失败不再重复 get_db()
        d = get_db()
        for cid in ids:
            try:
                ok, _ = _apply_mirror(cid)  # 函数内已写状态
                # 抽帧失败时返 False（不进入 ok 计数）
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:  # noqa: BLE001
                fail_count += 1
                logger.warning("[片段镜像] clip={} 失败：{}", cid, e)
                try:
                    _write_clip_failed(d, cid, str(e))
                except Exception as ee:  # noqa: BLE001
                    logger.error(
                        "[片段镜像] 写失败状态也失败 clip={} 原异常={} 二次异常={}",
                        cid, e, ee,
                    )
        logger.info("[片段镜像] 结束 成功 {} 失败 {} 总 {}", ok_count, fail_count, len(ids))

    threading.Thread(target=_run, args=(list(clip_ids),), daemon=True).start()


def _spawn_clip_render(clip_ids: list[str], project_id: str = "") -> str:
    """兼容别名（#385）：保留旧调用点，把片段投入切割队列，返回 bg_task_id。

    老调用方若不接返回值（_backfill / 复制项目）也无副作用；
    新调用方（add-clips 主路径）应接 bg_task_id 用于查询/取消。
    """
    return _spawn_clip_cut(clip_ids, project_id=project_id)


def _copy_asset(src_rel: str | None, dst_rel: str) -> str | None:
    """把 data 下的相对路径文件复制到新相对路径（同盘 IO）。

    参数:
        src_rel: 源相对路径；None 或源文件不存在时直接返回 None
        dst_rel: 目标相对路径（父目录自动创建）
    返回:
        成功返回 dst_rel，否则 None
    """
    if not src_rel:
        return None
    src_f = get_data_dir() / src_rel
    if not src_f.is_file():
        return None
    dst_f = get_data_dir() / dst_rel
    dst_f.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src_f, dst_f)
    except OSError:
        return None
    return dst_rel


def _cleanup_clip_files(rows: list[dict]) -> None:
    """删除片段关联的落盘文件（视频 + 缩略图）。

    rows 可为空列表（早返回）。file_path / thumb_path 为空时跳过
    （DB NOT NULL + 业务规则「添加即落盘」保证通常非空，仅极端历史脏数据可能为空）。
    """
    if not rows:
        return
    for row in rows:
        for rel in (row.get("file_path"), row.get("thumb_path")):
            if not rel:
                continue
            try:
                (get_data_dir() / rel).unlink(missing_ok=True)
            except OSError:
                continue


def reorder_clips(shot_id: str, ordered_clip_ids: list[str]) -> None:
    """重排分镜内片段顺序（#56 拖拽换位）。

    参数:
        shot_id: 分镜 ID
        ordered_clip_ids: 该分镜内全部片段 ID 的目标顺序
    """
    d = get_db()
    if not d.query_one("SELECT id FROM project_shot WHERE id=?", (shot_id,)):
        raise ValueError("分镜不存在")
    # 校验片段均属本分镜
    own = {r["id"] for r in d.query_all(
        "SELECT id FROM project_shot_clip WHERE shot_id=?", (shot_id,))}
    for cid in ordered_clip_ids:
        if cid not in own:
            raise ValueError("片段不属于该分镜")
    for idx, cid in enumerate(ordered_clip_ids):
        d.update_by_id("project_shot_clip", cid, {"sort_order": idx})


def move_clip(clip_id: str, target_shot_id: str) -> None:
    """片段移动到其他分镜（追加到目标分镜末尾）。"""
    d = get_db()
    if not d.query_one("SELECT id FROM project_shot WHERE id=?", (target_shot_id,)):
        raise ValueError("目标分镜不存在")
    _check_clip_limit(target_shot_id, 1)
    next_order = d.query_one(
        "SELECT COALESCE(MAX(sort_order),-1)+1 AS o FROM project_shot_clip WHERE shot_id=?",
        (target_shot_id,))["o"]
    d.update_by_id("project_shot_clip", clip_id,
                   {"shot_id": target_shot_id, "sort_order": next_order})


def delete_clip(clip_id: str) -> None:
    """删除片段（同时清理其片段文件与缩略图）。"""
    d = get_db()
    # 联表拿 project_id（删除后用于刷新 legal_total 缓存）
    clip = d.query_one(
        """SELECT c.*, s.project_id AS _pid FROM project_shot_clip c
           JOIN project_shot s ON c.shot_id=s.id WHERE c.id=?""",
        (clip_id,))
    if not clip:
        return
    d.execute("DELETE FROM project_shot_clip WHERE id=?", (clip_id,))
    _cleanup_clip_files([clip])
    if clip.get("_pid"):
        _safe_refresh_project_legal_total(clip["_pid"])


def delete_clips_by_material(project_id: str, material_ids: list[str]) -> int:
    """按素材 ID 批量删除指定项目内的全部对应片段（#399 / #修复-B）。

    仅删当前项目内 `project_shot_clip` 行 + 落盘片段文件/缩略图；素材库中的
    素材本体不动（其他项目可能仍引用）。命中 idx_clip_material 索引。

    参数:
        project_id: 项目 ID（限定范围，避免跨项目误删）
        material_ids: 素材 ID 列表（去重/为空时直接 return 0）
    返回:
        删除片段行数
    """
    if not material_ids:
        return 0
    if not project_id:
        raise ValueError("project_id 不能为空")
    # 去重避免 IN (...) 重复 + 无意义的清理
    ids = list({m for m in material_ids if m})
    d = get_db()
    placeholders = ",".join(["?"] * len(ids))
    rows = d.query_all(
        f"SELECT c.*, s.project_id AS _pid FROM project_shot_clip c "
        f"JOIN project_shot s ON c.shot_id=s.id "
        f"WHERE s.project_id=? AND c.material_id IN ({placeholders})",
        (project_id, *ids))
    if not rows:
        return 0
    _cleanup_clip_files(rows)
    clip_ids = [r["id"] for r in rows]
    clip_placeholders = ",".join(["?"] * len(clip_ids))
    d.execute(
        f"DELETE FROM project_shot_clip WHERE id IN ({clip_placeholders})",
        tuple(clip_ids))
    # 仅 refresh 限定项目（理论只一个，循环兼容历史脏数据）
    for pid in {r["_pid"] for r in rows if r.get("_pid")}:
        _safe_refresh_project_legal_total(pid)
    return len(rows)


def mirror_clip(clip_id: str) -> dict:
    """镜像片段（#57 / #380：原地切换，不新增片段）。

    先翻转 DB 的 mirrored（0↔1），再异步对片段 mp4 做 hflip 并重抽首帧。
    不再走原视频重切（拆分割和镜像）。再调用一次即镜像回原。
    必须 file_status='ready' 才能镜像（没分割好不能镜像）。

    镜像异步执行，返回时 file_status='pending'。
    """
    d = get_db()
    clip = d.query_one("SELECT * FROM project_shot_clip WHERE id=?", (clip_id,))
    if not clip:
        raise ValueError("片段不存在")
    if clip["file_status"] != "ready" or not clip["file_path"]:
        raise ValueError("片段未就绪，无法镜像（请先完成分割）")
    d.update_by_id("project_shot_clip", clip_id, {
        "mirrored": 1 - clip["mirrored"],
        "file_status": "pending",   # 待重渲染
        "fail_reason": None,
        "thumb_path": None,         # 旧帧未镜像，必须重抽
    })
    _spawn_clip_mirror([clip_id])
    return d.query_one("SELECT * FROM project_shot_clip WHERE id=?", (clip_id,))


def render_clip(clip_id: str) -> dict:
    """重新渲染片段文件（失败重试 / 缩略图丢失恢复，#380）。

    按当前 mirrored 状态分派：mirrored=0 → _cut_clip；mirrored=1 → _cut_clip + _apply_mirror。
    拆分割和镜像，但重试入口要保证视觉一致性（按当前 DB 镜像状态输出）。
    """
    d = get_db()
    clip = d.query_one("SELECT * FROM project_shot_clip WHERE id=?", (clip_id,))
    if not clip:
        raise ValueError("片段不存在")
    d.update_by_id("project_shot_clip", clip_id,
                   {"file_status": "pending", "fail_reason": None})
    _spawn_clip_retry(clip_id)
    return d.query_one("SELECT * FROM project_shot_clip WHERE id=?", (clip_id,))


def _spawn_clip_retry(clip_id: str) -> None:
    """手动重试线程：先判断分割是否成功 → 再判断抽帧是否成功 → 依次补完。
    mirrored=1 时在分割/抽帧都成功后跑 _apply_mirror。
    """
    logger.info("[片段重试] 启动后台线程 clip={}", clip_id)

    def _run(cid: str) -> None:
        d = get_db()
        clip = d.query_one(
            """SELECT c.*, s.project_id AS project_id
               FROM project_shot_clip c JOIN project_shot s ON c.shot_id=s.id
               WHERE c.id=?""", (cid,))
        if not clip:
            logger.warning("[片段重试] clip 不存在 cid={}", cid)
            return
        project_id = clip["project_id"]
        seq = int(clip["seq"] or 0) or _next_clip_seq(project_id)
        rel = clip["file_path"] or _clip_rel_path(project_id, seq, cid)
        thumb_rel = clip["thumb_path"] or _clip_thumb_rel(project_id, seq, cid)
        out = get_data_dir() / rel

        # 1) 分割未完成（out 不存在）→ 跑 _cut_clip
        #    _cut_clip 内部含抽帧（成功 ready / 失败 failed），一站式处理
        if not out.is_file():
            try:
                ok, reason = _cut_clip(cid)
            except Exception as e:  # noqa: BLE001
                logger.error("[片段重试] _cut_clip 异常 clip={} 异常={}", cid, e)
                try:
                    _write_clip_failed(d, cid, str(e))
                except Exception:
                    pass
                return
            if not ok:
                logger.warning("[片段重试] clip={} 分割仍失败 reason={}", cid, reason)
                return
            # _cut_clip 抽帧失败已返 False（与 _apply_mirror 抽帧失败语义一致），
            # 此处 file_status='failed' 兜底检查防御未来 _cut_clip 改回 True 时
            # _apply_mirror 覆盖 failed → ready/mirror_failed
            cur = d.query_one("SELECT file_status FROM project_shot_clip WHERE id=?", (cid,))
            if cur and cur["file_status"] != "ready":
                logger.warning("[片段重试] clip={} 抽帧仍失败，跳过镜像", cid)
                return
            # 分割 + 抽帧都 OK，按 mirrored 决定是否镜像
            if int(clip["mirrored"]):
                try:
                    mirror_ok, mirror_reason = _apply_mirror(cid)
                    if mirror_ok:
                        logger.info("[片段重试] clip={} cut+mirror 成功", cid)
                    else:
                        # _apply_mirror 内部已 _write_clip_failed / 设 thumb_path，
                        # 仅补失败原因日志便于排查
                        logger.warning("[片段重试] clip={} mirror 失败 reason={}", cid, mirror_reason)
                except Exception as e:  # noqa: BLE001
                    logger.error("[片段重试] clip={} mirror 异常：{}", cid, e)
                    _write_clip_failed(d, cid, f"镜像失败：{e}")
            else:
                logger.info("[片段重试] clip={} cut 成功（无需镜像）", cid)
            return

        # 2) 抽帧缺失（thumb_rel 为空 或 文件不存在）→ 单独跑抽帧（无需重切）
        thumb_out = get_data_dir() / thumb_rel if thumb_rel else None
        need_thumb = (not thumb_rel) or (thumb_out is None) or (not thumb_out.is_file())
        if need_thumb:
            # thumb_rel 缺失时按规则算一个（与 _cut_clip / _apply_mirror 一致）
            if not thumb_rel:
                thumb_rel = _clip_thumb_rel(project_id, seq, cid)
                thumb_out = get_data_dir() / thumb_rel
            _retry_clip_thumb(
                out, thumb_out, thumb_rel, d, cid,
                log_ok="[片段重试] 抽帧补全成功 cid={}",
                log_fail="[片段重试] 抽帧仍失败 cid={}",
            )
            return

        # 3) 分割 + 抽帧都齐备 → 防御性刷 ready + rev（正常 _cut_clip 已写过）
        #    防御：thumb_rel 字段被外部清空场景（外部 API 误改）→ is_file 为 False 时
        #    回退到第 2 段补抽帧路径，避免写 ready + thumb_path=空
        thumb_out = get_data_dir() / thumb_rel
        if not thumb_out.is_file():
            logger.warning("[片段重试] thumb_rel 非空但文件缺失 cid={}，回退补抽帧", cid)
            _retry_clip_thumb(out, thumb_out, thumb_rel, d, cid)
            return
        _commit_clip_thumb_result(d, cid, thumb_out, thumb_rel, True)
        logger.info("[片段重试] 已就绪跳过 cid={}", cid)

    threading.Thread(target=_run, args=(clip_id,), daemon=True).start()


def batch_clips(clip_ids: list[str], action: str,
                target_shot_id: str | None = None) -> dict:
    """片段批量操作（#57 分镜管理模式）。

    参数:
        clip_ids: 目标片段 ID 列表
        action: delete（删除）/ mirror（原地镜像）/ move（移动到其他分镜）/ retry_render（重渲染）
        target_shot_id: action=move 时必填
    返回:
        {"ok": True, "affected": n, "failed": [{"clip_id", "reason"}]}
    """
    if action not in ("delete", "mirror", "move", "retry_render"):
        raise ValueError("action 非法")
    if action == "move":
        if not target_shot_id:
            raise ValueError("移动需指定目标分镜")
        if not get_db().query_one("SELECT id FROM project_shot WHERE id=?", (target_shot_id,)):
            raise ValueError("目标分镜不存在")

    affected = 0
    failed: list[dict] = []
    for cid in clip_ids:
        try:
            if action == "delete":
                delete_clip(cid)
            elif action == "mirror":
                mirror_clip(cid)
            elif action == "move":
                move_clip(cid, target_shot_id or "")
            else:  # retry_render
                render_clip(cid)
            affected += 1
        except ValueError as e:
            failed.append({"clip_id": cid, "reason": str(e)})
        except Exception as e:  # noqa: BLE001 单条失败不影响其余
            failed.append({"clip_id": cid, "reason": str(e)[:200]})
    # #93：批量操作汇总日志（之前 only failed.append，运维查日志只能看到返回结构里的 failed）
    if failed:
        logger.warning(
            "[片段批量] action={} 处理 {} 条，失败 {} 条",
            action, len(clip_ids), len(failed),
        )
    return {"ok": True, "affected": affected, "failed": failed}


# ---------- BGM / 文案池 ----------

def add_bgms(project_id: str, material_ids: list[str]) -> int:
    """批量添加 BGM（音乐素材）。BGM 音量走项目级设置。"""
    d = get_db()
    added = 0
    order = d.query_one(
        "SELECT COALESCE(MAX(sort_order),-1)+1 AS o FROM project_bgm WHERE project_id=?",
        (project_id,))["o"]
    for mid in material_ids:
        material = material_service.get_material(mid)
        if not material or material["type"] != "music":
            continue
        d.insert("project_bgm", {"project_id": project_id, "material_id": mid,
                                  "sort_order": order})
        order += 1
        added += 1
    return added


def remove_bgm(project_id: str, material_id: str) -> None:
    """移除 BGM。"""
    get_db().execute("DELETE FROM project_bgm WHERE project_id=? AND material_id=?",
                     (project_id, material_id))


def list_bgms(project_id: str) -> list[dict]:
    """BGM 池列表（音量走项目级，本表不再带 bgm_volume 列）。"""
    return get_db().query_all(
        """SELECT b.*, m.title, m.duration_ms, m.file_path, m.file_status
           FROM project_bgm b LEFT JOIN material m ON b.material_id=m.id
           WHERE b.project_id=? ORDER BY b.sort_order""", (project_id,))


def list_text_pool(pool_type: str, scope: str, project_id: str | None = None) -> list[dict]:
    """文案池列表（title/topic；global/project 两层）。"""
    d = get_db()
    where = "WHERE pool_type=? AND scope=? AND deleted=0"
    params: list = [pool_type, scope]
    if scope == "project":
        where += " AND project_id=?"
        params.append(project_id)
    return d.query_all(f"SELECT * FROM text_pool {where} ORDER BY create_time", tuple(params))


def add_text_pool(pool_type: str, contents: list[str], scope: str, project_id: str | None = None) -> int:
    """批量添加文案池条目（每行一条）。"""
    d = get_db()
    added = 0
    for content in contents:
        content = content.strip()
        if not content:
            continue
        d.insert("text_pool", {
            "pool_type": pool_type, "scope": scope,
            "project_id": project_id if scope == "project" else None,
            "content": content, "enabled": 1})
        added += 1
    return added


def delete_text_pool(entry_id: str) -> None:
    """删除文案池条目。"""
    get_db().soft_delete_by_id("text_pool", entry_id)


def random_text(pool_type: str, project_id: str) -> str | None:
    """随机取文案（项目池 + 全局池合并，F-04.8）。"""
    import random as _random
    entries = list_text_pool(pool_type, "global") + \
        list_text_pool(pool_type, "project", project_id)
    enabled = [e["content"] for e in entries if e["enabled"]]
    return _random.choice(enabled) if enabled else None


# ---------- 组合与生成（F-04.9） ----------

# 均匀项目基准值实测锚点：(分镜数, 每分镜片段数) -> 可生成数。
# 倒推过程中实测到但闭式尚未闭合的少数点，先落表。
_UNIFORM_ANCHORS: dict[tuple[int, int], int] = {
    (6, 6): 211,
    (6, 7): 336,
    (7, 7): 744,
}


def _uniform_count(n: int, c: int) -> int:
    """均匀项目（n 个分镜、每分镜 c 个候选片段）的可生成数量。

    由 82 条实测样例倒推所得规律：
        n<=2 : c
        n==3 : c² − 1
        n==4 : c(c²−1)/3
        n==5 : 2·V(4) + 1
        n>=6 : 实测锚点优先；缺失时按 V(k)=V(k−1)+V(k−2) 外推（待实测校准）
    """
    if n <= 0 or c <= 0:
        return 0
    if c == 1:
        return 1        # 每分镜只有 1 个候选 → 唯一组合
    if n <= 2:
        return c
    v3 = c * c - 1
    if n == 3:
        return v3
    v4 = c * v3 // 3            # c(c²−1) 恒被 3 整除
    if n == 4:
        return v4
    v5 = 2 * v4 + 1
    if n == 5:
        return v5
    if (n, c) in _UNIFORM_ANCHORS:
        return _UNIFORM_ANCHORS[(n, c)]
    a, b = v4, v5
    for _ in range(6, n + 1):
        a, b = b, a + b
    return b


def generatable_count(scene_clip_counts: list[int]) -> int:
    """可生成视频数量（倒推规则，替代同源防重口径）。

    规则:
        m = 最小片段数（0 片段的分镜不计入）
        C = 最大片段数，n = 有效分镜数
        值 = floor( m × V(n, C) / C )，其中 V 为"全部分镜都补到 C 个"时的值

    参数:
        scene_clip_counts: 各分镜的候选片段数
    返回:
        可生成数量（0 表示存在空分镜或无可生成组合）
    """
    sizes = [c for c in scene_clip_counts if c > 0]
    if not sizes:
        return 0
    n = len(sizes)
    m, C = min(sizes), max(sizes)
    v = _uniform_count(n, C)
    if m == C:
        return v
    return m * v // C


def _safe_refresh_project_legal_total(project_id: str) -> None:
    """刷新 legal_total 缓存（失败仅警告，不阻塞主流程）。

    加/删分镜/片段接口末尾调用，避免后续读实时算。
    """
    try:
        refresh_project_legal_total(project_id)
    except Exception as e:  # noqa: BLE001 失败回退实时算，不影响用户操作
        logger.warning(f"[legal_total] 刷新失败 project={project_id}: {e}")


def _calc_legal_combination(project_id: str) -> int:
    """纯计算可生成数（O(n) 倒推算法，不写 DB）。"""
    d = get_db()
    counts = [r["c"] for r in d.query_all(
        """SELECT COUNT(*) AS c FROM project_shot_clip c
           JOIN project_shot s ON c.shot_id=s.id
           WHERE s.project_id=? GROUP BY s.id ORDER BY s.sort_order""", (project_id,))]
    if not counts or any(c == 0 for c in counts):
        return 0
    return min(generatable_count(counts), MAX_COMBINATIONS)


def refresh_project_legal_total(project_id: str) -> int:
    """重算并写回 project.legal_total（加/删分镜/片段接口末尾调用）。"""
    new_value = _calc_legal_combination(project_id)
    d = get_db()
    d.update_by_id("project", project_id, {"legal_total": new_value})
    return new_value


def _legal_combination_total(project_id: str) -> int:
    """读取项目可生成数（优先字段缓存；字段=0 时回退计算 + 写回）。"""
    d = get_db()
    row = d.query_one("SELECT legal_total FROM project WHERE id=?", (project_id,))
    if row is None:
        return 0
    cached = int(row.get("legal_total") or 0)
    if cached > 0:
        return cached
    # 字段=0（旧项目未回填 / 新建项目）：回退计算并写回
    new_value = _calc_legal_combination(project_id)
    if new_value > 0:
        d.update_by_id("project", project_id, {"legal_total": new_value})
    return new_value


def combination_available(project_id: str) -> dict:
    """可生成视频数量（#64）：合法组合总数 − 已生成数。

    返回:
        {"legal_total": 合法组合总数, "generated": 已生成数, "available": 可生成数}
    """
    d = get_db()
    legal = _legal_combination_total(project_id)
    # #413：已生成数改读 project.generated_total 字段（累计，删除成品不减）
    proj = d.query_one("SELECT generated_total FROM project WHERE id=?", (project_id,))
    generated = int((proj or {}).get("generated_total") or 0)
    return {"legal_total": legal, "generated": generated,
            "available": max(0, legal - generated)}


def _pick_combinations(project_id: str, count: int,
                       info: "TaskInfo | None" = None) -> list[list[dict]]:
    """接受-拒绝采样挑选组合（v25 新算法）。

    算法定义见 docs/视频组合算法约束.md：
      约束 1：同源率 ≤ 50%（同一 source_index 片段不超过半数）
      约束 2：位置重叠 ≤ 50%（与已生成组合比，全部位置）
      约束 3：按权重 max(0, 10000 - used_count) 采样，同分镜下权重最高片段被选
      约束 4：同分镜下权重相同的片段 random.choice 均匀随机

    不再做笛卡尔积全量枚举——每轮每个分镜按权重采样 1 片段，
    校验约束 1/2 通过则保留，否则重抽。连续失败超阈值则停止。

    返回: 组合列表,每项为按分镜序的片段 dict 列表
    """
    import random as _random

    d = get_db()
    shots = list_shots(project_id)
    if not shots or any(s["clip_count"] == 0 for s in shots):
        raise ValueError("存在无候选片段的分镜,无法生成")
    n_shots = len(shots)

    # #418：过滤掉未就绪或文件缺失的片段（file_status != ready / 文件不存在）
    # file_path 必有（v28 INSERT 预写）；not rel 防御保留（历史脏数据兜底）
    def _filter_ready_clips(shot_clips: list[dict]) -> list[dict]:
        ready = []
        for c in shot_clips:
            if c.get("file_status") != "ready":
                continue
            rel = c.get("file_path")
            if not rel:
                continue
            if not (get_data_dir() / rel).is_file():
                continue
            ready.append(c)
        return ready

    pools = [_filter_ready_clips(s["clips"]) for s in shots]
    # 过滤后某分镜无候选 → 抛错（与原"无候选"语义一致）
    if any(len(pool) == 0 for pool in pools):
        raise ValueError("存在无可用片段的分镜,无法生成")

    # 扩容隔离: 若项目当前 shot_count < 实际分镜数,清空已生成
    # （避免扩容后 K/N > 50% 时大部分组合被约束 2 拒，无法生成新视频）
    # 同步清 generated_total 字段（#413：累计数与 generated_video 行数保持一致）
    project = d.query_one("SELECT title, shot_count FROM project WHERE id=?", (project_id,))
    title = (project or {}).get("title", project_id)
    prev_shot_count = (project or {}).get("shot_count", 0) or 0
    if prev_shot_count > 0 and prev_shot_count < n_shots:
        # execute() 返 rowcount（删除行数）
        cleared = d.execute("DELETE FROM generated_video WHERE project_id=?", (project_id,))
        d.execute("UPDATE project SET generated_total=0 WHERE id=?", (project_id,))
        logger.info(
            f"[生成视频] 项目名称：{title}，视频数：0 / {count}"
            f"（扩容 {prev_shot_count}→{n_shots}，清空 {cleared} 条历史）")

    # 已生成组合键集合 + 列表（集合用于"完全相同直接跳过"，列表用于约束 2 位置重叠比对）
    generated_set = {
        g["combination_key"] for g in d.query_all(
            "SELECT combination_key FROM generated_video WHERE project_id=?",
            (project_id,))
    }
    generated_list = [k.split("-") for k in generated_set]
    n_history = len(generated_set)
    # 任务 #70：删"选组合开始"日志——紧接着行 1862/1858 "选组合完成"已带"视频数 X/Y"，
    # 这条"0/X"开头对用户无意义且与后续汇总重复

    def key_of(combo: list[dict]) -> str:
        return "-".join(c["id"] for c in combo)

    def is_same_source_ok(combo: list[dict]) -> bool:
        """约束 1：同源率 ≤ 50% 校验。"""
        sources = [c.get("source_index", 0) or 0 for c in combo]
        if not sources:
            return True
        max_same = max(sources.count(s) for s in set(sources))
        return max_same / len(combo) <= 0.5

    def overlap_with_generated(combo_ids: list[str]) -> bool:
        """约束 2：与任一已生成组合位置重叠率 > 50% 即拒。

        同分镜数（K == N）：same / N > 0.5 拒。
        扩容（K < N）：比新视频前 K 段，same / N > 0.5 拒（前缀比保守近似，
        假设历史 K 段位于新视频前 K 位）。K 越接近 N，被拒概率越大。
        """
        n = len(combo_ids)
        for g_ids in generated_list:
            k = len(g_ids)
            if k > n:
                continue  # 历史比新视频还长，不可能
            if k == n:
                same = sum(1 for a, b in zip(combo_ids, g_ids) if a == b)
                if same / n > 0.5:
                    return True
            else:
                # 扩容 K < N：比前 K 段，重叠率除以 N
                same = sum(1 for a, b in zip(combo_ids[:k], g_ids) if a == b)
                if same / n > 0.5:
                    return True
        return False

    def pick_one_by_weight(shot_clips: list[dict]) -> dict:
        """约束 3+4：按权重 max(0, 10000 - used_count) 采样，同权重 random.choice。"""
        weights = [max(0, 10000 - (c.get("used_count") or 0)) for c in shot_clips]
        max_w = max(weights)
        top = [c for c, w in zip(shot_clips, weights) if w == max_w]
        return _random.choice(top)

    # 接受-拒绝采样循环
    picked: list[list[dict]] = []
    picked_keys: set[str] = set()
    # 耗尽保护：连续失败次数上限（防止死循环，组合空间耗尽就停）
    consecutive_fail = 0
    max_consecutive_fail = count * 20 + 100
    # 拒绝原因分类计数（任务完成后打日志用）
    fail_dup_key = 0       # 已选 / 历史已存在
    fail_same_source = 0   # 约束 1
    fail_overlap = 0       # 约束 2

    while len(picked) < count:
        # 取消信号：用户在前端点取消时及时退出（前端 cancel_requested=True）
        if info is not None and info.cancel_requested:
            break
        if consecutive_fail >= max_consecutive_fail:
            break
        # 每个分镜按权重采样 1 片段
        combo = [pick_one_by_weight(pool) for pool in pools]
        k = key_of(combo)
        # 已选过 / 历史已存在 → 重抽（兜底：约束 2 在 K==N 时 same/N=100% 必拒，但显式跳过更省计算）
        if k in picked_keys or k in generated_set:
            consecutive_fail += 1
            fail_dup_key += 1
            continue
        # 约束 1：同源率 ≤ 50%
        if not is_same_source_ok(combo):
            consecutive_fail += 1
            fail_same_source += 1
            continue
        # 约束 2：位置重叠 ≤ 50%
        combo_ids = [c["id"] for c in combo]
        if overlap_with_generated(combo_ids):
            consecutive_fail += 1
            fail_overlap += 1
            continue
        picked.append(combo)
        picked_keys.add(k)
        consecutive_fail = 0

    # 选组合结果日志
    cancel_triggered = info is not None and info.cancel_requested
    exhausted = consecutive_fail >= max_consecutive_fail
    # #97：选组合瓶颈摘要塞到 info.message，状态栏直观看到拒绝原因分布
    # （仅非预期路径；cancel 路径前端按钮已知、info.message 留空）
    if cancel_triggered:
        logger.info(
            f"[生成视频] 项目名称：{title}，视频数：{len(picked)} / {count}"
            f"（选组合取消，已拒 key={fail_dup_key} 同源={fail_same_source} 重叠={fail_overlap}）")
    elif exhausted:
        logger.warning(
            f"[生成视频] 项目名称：{title}，视频数：{len(picked)} / {count}"
            f"（选组合耗尽，连续失败 {consecutive_fail} 次触发保护，"
            f"已拒 key={fail_dup_key} 同源={fail_same_source} 重叠={fail_overlap}）")
        if info is not None:
            info.message = "可用视频组合已用完（重复被拒过多），已生成部分视频"
    elif len(picked) < count:
        # 正常路径但凑不够（剩余可生成数确实不够 count）
        logger.warning(
            f"[生成视频] 项目名称：{title}，视频数：{len(picked)} / {count}"
            f"（选组合不足，已拒 key={fail_dup_key} 同源={fail_same_source} 重叠={fail_overlap}）")
        if info is not None:
            info.message = f"可用视频组合不足，仅生成 {len(picked)}/{count} 条"
    else:
        logger.info(
            f"[生成视频] 项目名称：{title}，视频数：{len(picked)} / {count}"
            f"（选组合完成，已拒 key={fail_dup_key} 同源={fail_same_source} 重叠={fail_overlap}）")

    # 更新项目分镜计数
    if prev_shot_count != n_shots:
        d.update_by_id("project", project_id, {"shot_count": n_shots})
        _safe_refresh_project_legal_total(project_id)

    return picked


def create_generate_task(project_id: str, count: int) -> dict:
    """创建生成任务（入队执行）。"""
    d = get_db()
    project = d.query_one("SELECT * FROM project WHERE id=? AND deleted=0", (project_id,))
    if not project:
        raise ValueError("项目不存在")
    # 入参基本校验（前置；先于 available 校验，避免 count=0 误报"素材池耗尽"）
    if count < 1:
        raise ValueError("本批生成数量至少 1 条")
    # 以「可生成数（倒推规则）」为准（#64）
    legal = _legal_combination_total(project_id)
    if legal == 0:
        raise ValueError("存在无候选片段的分镜，无法生成")
    # #413：已生成数改读 project.generated_total 字段（累计，删除成品不减）
    generated = int(project.get("generated_total") or 0)
    available = legal - generated
    if available <= 0:
        raise ValueError("素材池耗尽（可生成组合已全部生成），请扩充素材")
    # #410 拦截条件：已生成 + 本批 > 最大可生成数（即 count > available = legal - generated）
    # legal 已被 MAX_COMBINATIONS 截断（项目一生累计上限 = 10000）
    if count > available:
        raise ValueError(
            f"本批生成数量 {count} 超过剩余可生成数 {available}（最大可生成 {legal}，已生成 {generated}），请调整后再提交")
    task_id = d.insert("generate_task", {
        "project_id": project_id, "plan_count": count, "status": "waiting"})
    # 投递后台执行
    # #411：任务队列中"视频生成"类型直接展示项目名称
    bg_id = task_service.submit("generate", project['title'],
                                lambda info: _run_generate(task_id, info))
    # 任务 #70：删"任务创建"日志——task_service.submit 内部已打 [任务开始]，
    # 这里再"0/X"开头是重复且误导
    d.update_by_id("generate_task", task_id, {"status": "running"})
    return {"generate_task_id": task_id, "bg_task_id": bg_id, "plan_count": count}


def _run_generate(generate_task_id: str, info) -> str:
    """执行生成任务：逐组合 切割→拼接→BGM→去重→落库。"""
    d = get_db()
    task = d.query_one("SELECT * FROM generate_task WHERE id=?", (generate_task_id,))
    if not task:
        return "任务不存在"
    project = d.query_one("SELECT * FROM project WHERE id=?", (task["project_id"],))
    dedup_rules = json.loads(project["dedup_rules_json"] or "{}")
    # #427：优先用 width/height（项目创建后规范字段），output_resolution 仅作兼容 fallback
    proj_w = int(project.get("width") or 0)
    proj_h = int(project.get("height") or 0)
    if proj_w > 0 and proj_h > 0:
        resolution = f"{proj_w}x{proj_h}"
    else:
        resolution = project.get("output_resolution") or "1080x1920"
    # #多画面适配：项目级 fit_mode（cover/contain/fill/blur_bg）
    fit_mode = project.get("fit_mode") or "cover"
    # #96：项目级"保留原视频声音"开关（默认 0=不保留）
    keep_original_audio = bool(project.get("keep_original_audio", 0))
    # 项目级 BGM 音量比例（#99 默认 1.0=100%，与 BGM 原音量一致）
    bgm_volume = float(project.get("bgm_volume", 1.0) or 1.0)
    bgms = list_bgms(project["id"])

    # 校验 FFmpeg 可用
    from app.core.ffmpeg import ffmpeg_available
    if not ffmpeg_available():
        d.update_by_id("generate_task", generate_task_id,
                       {"status": "failed", "fail_detail_json": json.dumps({"reason": "FFmpeg 不可用"})})
        # #P0-10：worker 必须返回契约值（success/partial/failed），不再返字符串
        info.message = "FFmpeg 不可用"
        return "failed"

    # PR3 #56：进度模板统一，加 start_ts 算耗时
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts
    start_ts = info.start_ts or time.time()

    # 选组合（info 用于接收取消信号）
    try:
        combos = _pick_combinations(project["id"], task["plan_count"], info=info)
    except ValueError as e:
        d.update_by_id("generate_task", generate_task_id,
                       {"status": "failed", "fail_detail_json": json.dumps({"reason": str(e)})})
        # #P0-10：worker 返契约值 + info.message 写明失败原因
        info.message = str(e)
        return "failed"

    # 选组合阶段被取消 → 任务标记 cancelled，不进入主循环
    if info is not None and info.cancel_requested:
        d.update_by_id("generate_task", generate_task_id,
                       {"status": "cancelled",
                        "fail_detail_json": json.dumps({"reason": "选组合阶段已取消"})})
        logger.info(f"[生成视频] 项目名称：{project['title']}，视频数：0 / {task['plan_count']}"
                    f"（任务取消，选组合阶段已中止）")
        return "cancelled"

    # 选组合实际拿到 < 请求（耗尽 / 取消之外） → 警告
    if len(combos) < task["plan_count"]:
        logger.warning(f"[生成视频] 项目名称：{project['title']}，视频数：{len(combos)} / {task['plan_count']}"
                       f"（实际拿到少于计划，将按 {len(combos)} 个组合继续）")

    done = fail = 0
    fails: list[dict] = []
    finished_dir = get_data_dir() / "finished" / project["id"]
    temp_dir = get_data_dir() / "temp" / f"gen_{generate_task_id[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    # 任务 #76：源头补音频的临时 mp4 路径列表（任务结束 shutil.rmtree(temp_dir) 一起清）
    padded_clips: list[str] = []

    # 任务 #70：删"worker 开始"INFO——task_service._run 已在 status=running 时打 [任务开始]，
    # 紧接着每视频循环 info.progress 已覆盖，这里"0/N"开头是冗余

    # 任务 #41：连续 ffmpeg 失败熔断（避免坏 encoder 反复撞墙 + CPU 100% 死循环）
    _FFMPEG_FAIL_LIMIT = 5
    _ffmpeg_fail_streak = 0

    def _on_ffmpeg_result(ok: bool, combo_idx: int, stage: str) -> None:
        """任务级熔断：连续失败即抛 RuntimeError 终止整任务。

        ok=True 重置计数；ok=False 自增,达阈值抛错,外层 _run 捕获后状态 'failed'。
        """
        nonlocal _ffmpeg_fail_streak
        if ok:
            _ffmpeg_fail_streak = 0
            return
        _ffmpeg_fail_streak += 1
        fails.append({"index": combo_idx, "reason": f"{stage}失败(连续{_ffmpeg_fail_streak}次)"})
        if _ffmpeg_fail_streak >= _FFMPEG_FAIL_LIMIT:
            raise RuntimeError(
                f"FFmpeg 连续失败 {_ffmpeg_fail_streak} 次,任务熔断。"
                f"请检查 ffmpeg/libx264 二进制或源素材"
            )

    for i, combo in enumerate(combos):
        raise_for_cancel(info)
        # PR3 #56：进度模板统一（实时即终态）
        elapsed = int(time.time() - start_ts)
        info.progress = f"已生成 {done + fail}/{len(combos)}，用时 {_fmt_hms(elapsed)}"
        # 任务 #67：删每个视频完成都打 INFO 的循环日志（ffmpeg 成功完成无需逐条刷屏）。
        # 异常路径仍由外层 logger.warning/error（行 2103 熔断 / 行 2146 失败明细）覆盖。
        try:
            # 1. 取片段文件（#418：文件不存在不再兜底即时切割，直接 fail 该视频）
            clip_files: list[str] = []
            ok = True
            fail_reason_clip = ""
            for j, clip in enumerate(combo):
                rel = clip.get("file_path")
                # 过滤条件：file_status != ready / 文件不存在 → 标记失败
                # file_path 必有（v28 INSERT 预写）；not rel 防御保留（历史脏数据兜底）
                if not rel or clip.get("file_status") != "ready":
                    ok = False
                    fail_reason_clip = f"片段 {j + 1} 文件不存在"
                    break
                persistent = get_data_dir() / rel
                if not persistent.is_file():
                    ok = False
                    fail_reason_clip = f"片段 {j + 1} 文件不存在"
                    break
                clip_path = str(persistent)
                # 任务 #76：单视频直进 render 不经过 cut_clip——这里源头补音频，
                # 让 render_video_full 不用再追加 lavfi input slot（统一逻辑）
                # #82：单条补成功降 debug（200+ clip 项目会刷屏），失败仍 WARNING
                # 留上下文；汇总条数改在任务收尾处统一 INFO 输出。
                streams = video_edit._probe_clip_streams(clip_path)
                if streams["has_video"] and not streams["has_audio"]:
                    padded = temp_dir / f"padded_{i}_{j}_{persistent.name}"
                    if video_edit.ensure_clip_has_audio(clip_path, str(padded)):
                        clip_path = str(padded)
                        padded_clips.append(clip_path)
                        logger.debug("[生成] clip {} 缺音频 → 补 lavfi", persistent.name)
                    else:
                        logger.warning(
                            "[生成] clip {} 补音频失败，render_video_full 兜底追加 lavfi input",
                            persistent.name,
                        )
                clip_files.append(clip_path)
            if not ok:
                fail += 1
                if not fails or fails[-1].get("index") != i:
                    fails.append({"index": i, "reason": fail_reason_clip})
                # 单条失败 INFO：与成功路径同粒度（片段文件缺失）
                logger.info(
                    "[生成视频] {}/{} 失败：{}",
                    done + fail, len(combos), fail_reason_clip,
                )
                continue
            # 2. 合并渲染（D 优化：拼接+BGM混音+去重 单次 ffmpeg 调用）
            final_out = str(finished_dir / f"{done + 1}_{now_str().replace(':', '').replace(' ', '').replace('-', '')}.mp4")
            Path(final_out).parent.mkdir(parents=True, exist_ok=True)

            # BGM 选择（按 bgm_strategy）
            bgm_path = None
            bgm_material_id = None
            if bgms:
                import random as _random
                if project["bgm_strategy"] == "random":
                    bgm = _random.choice(bgms)
                else:
                    bgm = bgms[(done + fail) % len(bgms)]
                if bgm["file_status"] == "normal":
                    bgm_path = str(material_service.full_path_of(bgm))
                    bgm_material_id = bgm["material_id"]

            # #101：去重参数（#111 水印内容来源需项目名 + 生成时刻）
            generate_time_str = now_str()[:16]
            dedup_params = video_edit.build_dedup_params(
                dedup_rules,
                project_title=project.get("title", ""),
                generate_time_str=generate_time_str,
            )

            # 单次 ffmpeg：normalize → concat → 去重 → BGM 混音（失败直接更新状态，用户重试）
            render_ok = video_edit.render_video_full(
                clip_paths=clip_files,
                out_path=final_out,
                resolution=resolution,
                bgm_path=bgm_path,
                bgm_volume=bgm_volume,
                keep_original_audio=keep_original_audio,
                dedup_params=dedup_params,
                fit_mode=fit_mode,
            )
            _on_ffmpeg_result(render_ok, i, "渲染")
            if not render_ok:
                fail += 1
                if not fails or fails[-1].get("index") != i:
                    fails.append({"index": i, "reason": "渲染失败（拼接/BGM/去重 单次 ffmpeg）"})
                # 单条失败 INFO：与成功路径同粒度，便于逐条追踪
                logger.info(
                    "[生成视频] {}/{} 失败：渲染失败（拼接/BGM/去重 单次 ffmpeg）",
                    done + fail, len(combos),
                )
                continue
            # 5. 落库（组合键唯一索引兜底防重复；MD5 撞库已去掉：v25 算法下不会重复）
            duration_ms = video_edit.probe_duration_ms(final_out)
            combination_key = "-".join(c["id"] for c in combo)
            try:
                # 事务化落库（E 优化）：3 个 SQL 包事务，失败整体 ROLLBACK，
                # 不再需要 DELETE 补偿；保证 generated_total 与 used_count 同步一致。
                with d.transaction():
                    d.insert("generated_video", {
                        "project_id": project["id"], "combination_key": combination_key,
                        "file_path": str(Path(final_out).relative_to(get_data_dir())).replace("\\", "/"),
                        "file_size": Path(final_out).stat().st_size,
                        "duration_ms": duration_ms,
                        "bgm_material_id": bgm_material_id,
                        "dedup_params_json": json.dumps(dedup_params, ensure_ascii=False),
                        "status": "idle",
                    })
                    # #413：累计生成数落项目表（删除成品视频时不变）
                    d.execute("UPDATE project SET generated_total = generated_total + 1 WHERE id=?",
                              (project["id"],))
                    # v25：维护片段 used_count（被选中 +1，下一轮按权重 max(0, 10000-used_count) 采样降权）
                    clip_ids = [c["id"] for c in combo]
                    if clip_ids:
                        placeholders = ",".join("?" * len(clip_ids))
                        d.execute(
                            f"UPDATE project_shot_clip SET used_count = used_count + 1 "
                            f"WHERE id IN ({placeholders})",
                            tuple(clip_ids),
                        )
                done += 1
            except Exception as e:  # 事务 ROLLBACK 已自动回滚，无须 DELETE 补偿
                # #81：原 except 静默 fail += 1 不打 logger，前端看不到事务失败细节
                # （组合键重复 / 写盘满 / DB 锁），只看到 fail++ 一个数字。
                logger.warning(
                    "[生成视频] 落库失败 combo={} key={} 异常={}",
                    i, combination_key, e,
                )
                fail += 1
                fails.append({"index": i, "reason": f"组合键重复：{e}"})
                Path(final_out).unlink(missing_ok=True)
            # D 优化：单次 ffmpeg 无中间产物，仅 final_out（成品已落库或失败时已删）
            d.update_by_id("generate_task", generate_task_id,
                           {"done_count": done, "fail_count": fail})
            # 单条成功 INFO：序号/总数 + 输出文件名 + 用时
            # （任务 #67 原删除全部单条 INFO 改 10 条聚合，但用户反馈要看单条维度日志；
            # 成功路径每条一行，文件路径只取 basename 不刷屏）
            logger.info(
                "[生成视频] {}/{} 成功 → {}（用时 {}）",
                done + fail, len(combos),
                Path(final_out).name,
                _fmt_hms(int(time.time() - start_ts)),
            )
        except RuntimeError as e:
            # 任务 #41：FFmpeg 连续失败熔断触发,跳出循环,任务标记 failed
            logger.error("[生成任务熔断] task_id={} {}", generate_task_id, e)
            fails.append({"index": i, "reason": f"任务熔断:{e}"})
            fail += 1
            d.update_by_id("generate_task", generate_task_id,
                           {"done_count": done, "fail_count": fail,
                            "status": "failed",
                            "fail_detail_json": json.dumps(fails, ensure_ascii=False)})
            info.message = f"视频处理多次失败，已停止：{e}"
            shutil.rmtree(temp_dir, ignore_errors=True)
            notifier.notify("error", "task", f"生成任务熔断:{project['title']}",
                            f"FFmpeg 连续失败 {_FFMPEG_FAIL_LIMIT} 次,任务已停止")
            return "failed"
        except Exception as e:  # noqa: BLE001 单条失败不终止
            fail += 1
            fails.append({"index": i, "reason": str(e)})
            d.update_by_id("generate_task", generate_task_id,
                           {"done_count": done, "fail_count": fail})
        finally:
            # #83：单条结束立即刷一次 progress（无论 done/fail/熔断/异常都覆盖），
            # 避免状态栏滞后一帧（顶部刷新用的还是上一轮 done+fail）。
            # RuntimeError 也会走到这里，让熔断那一刻的"X/N"被前端捕到。
            # #85：elapsed 在 try 顶部才赋值，RuntimeError 熔断分支（return "failed"
            # 前）finally 仍跑，NameError 会抛；finally 必须在开头给 elapsed 默认值。
            elapsed = 0
            try:
                elapsed = int(time.time() - start_ts)
                info.progress = f"已生成 {done + fail}/{len(combos)}，用时 {_fmt_hms(elapsed)}"
            except Exception as e:  # noqa: BLE001 finally 内异常绝不能冒泡
                # #87：与 #77 回归一致，静默 pass 改为 debug，方便追根
                logger.debug("[生成视频] finally 刷 progress 失败：{}", e)
            # #84：每 10 个 combo 聚合 INFO（替代逐条 INFO 刷屏，比"开始/结束"两点更精细）
            processed = done + fail
            if processed > 0 and processed % 10 == 0:
                logger.info(
                    f"[生成视频] 项目名称：{project['title']}，"
                    f"进度 {processed}/{len(combos)}（成功 {done}，失败 {fail}），"
                    f"用时 {_fmt_hms(elapsed)}"
                )

    # 收尾：清理 temp 子目录
    shutil.rmtree(temp_dir, ignore_errors=True)
    status = "finished"
    d.update_by_id("generate_task", generate_task_id, {
        "done_count": done, "fail_count": fail, "status": status,
        "fail_detail_json": json.dumps(fails, ensure_ascii=False) if fails else None})
    # PR3 #56：实时即终态，最后一次循环内的 info.progress 即为终态（删除"成功{done} 失败{fail}"覆写）
    # 任务 #64：原日志"视频数 13/20（完成，成功 13 失败 7）"重复一次 done，
    # 且"视频数"含义模糊；改为"已处理 done+fail" + 括号细分成功/失败
    # #82：源头补音频条数汇总（之前每 clip 一行 INFO 200 条刷屏）
    if padded_clips:
        logger.info(
            f"[生成视频] 项目名称：{project['title']}，源头补音频：{len(padded_clips)} 个 clip（已混静音轨）"
        )
    logger.info(
        f"[生成视频] 项目名称：{project['title']}，"
        f"视频数：{done + fail} / {len(combos)}（成功 {done}，失败 {fail}）"
    )
    if fail > 0:
        # 失败明细摘要：按 reason 聚合（避免日志雪崩）
        from collections import Counter as _Counter
        reason_counter = _Counter()
        for f in fails:
            reason = f.get("reason", "未知")
            # 去熔断前缀 "[stage]失败(连续N次)" → 截断到"stage失败"
            for prefix in ("切割失败", "拼接失败", "去重处理失败"):
                if prefix in reason:
                    reason = prefix
                    break
            reason_counter[reason] += 1
        logger.warning(
            f"[生成视频] 项目名称：{project['title']}，失败明细 {fail} 条："
            + "，".join(f"{r}×{c}" for r, c in reason_counter.most_common()))
    notifier.notify("info" if fail == 0 else "warn", "task",
                    f"生成任务完成：{project['title']}", f"成功 {done} / 失败 {fail}")
    # 任务 #68：partial 时 info.message 兜底为"失败 N 条"（之前 task_service 兜底
    # 把 result="partial" 原样写到 message，前端信息列只能看到字面 "partial"）
    if fail > 0:
        # 把失败原因分组（最多 3 类）拼到 message，前端信息列可见具体原因
        if reason_counter:
            hints = "，".join(f"{r}×{c}" for r, c in reason_counter.most_common(3))
            info.message = f"部分失败：{hints}"
        else:
            info.message = f"失败 {fail} 条"
    return "success" if fail == 0 else "partial"


# ---------- 成品管理（F-04.10 / F-04-R15） ----------

def list_generated_videos(project_id: str = "", status: str = "",
                          page: int = 1, page_size: int = 20) -> dict:
    """成品视频列表（含发布情况：#68）。

    status 为成品占用状态：idle=未占用 / occupied=已被发布明细占用。
    另带出发布信息：已成功发布则取发布记录（账号/时间/线上视频ID），
    否则取进行中的发布明细（计划账号/计划时间/明细状态）。
    """
    d = get_db()
    where = "WHERE 1=1"
    params: list = []
    if project_id:
        where += " AND v.project_id=?"
        params.append(project_id)
    if status:
        where += " AND v.status=?"
        params.append(status)
    result = d.query_page(
        f"""SELECT v.*, t.title AS project_title FROM generated_video v
            LEFT JOIN project t ON v.project_id=t.id {where}
            ORDER BY v.create_time DESC""",
        tuple(params), page, page_size)
    for row in result["list"]:
        _attach_publish_info(d, row)
    return result


def _attach_publish_info(d, row: dict) -> None:
    """为成品行补充发布信息（发布记录优先，其次进行中的发布明细）。"""
    vid = row["id"]
    # 1) 已成功发布的留痕（最新一条）
    rec = d.query_one(
        """SELECT r.publish_time, r.account_id, r.account_snapshot, r.online_video_id,
                  a.nickname AS account_nickname
           FROM publish_record r LEFT JOIN account a ON r.account_id=a.id
           WHERE r.video_id=? AND r.status='success'
           ORDER BY r.publish_time DESC LIMIT 1""",
        (vid,))
    if rec:
        row["publish_state"] = "published"
        row["publish_account"] = rec["account_nickname"] or rec["account_snapshot"] or rec["account_id"]
        row["publish_time"] = rec["publish_time"]
        row["online_video_id"] = rec["online_video_id"]
        return
    # 2) 进行中的发布明细（等待/发布中/挂起/失败）
    it = d.query_one(
        """SELECT i.status, i.plan_time, i.account_id, i.fail_reason,
                  a.nickname AS account_nickname
           FROM publish_task_item i LEFT JOIN account a ON i.account_id=a.id
           WHERE i.video_id=? ORDER BY i.plan_time DESC LIMIT 1""",
        (vid,))
    if it:
        row["publish_state"] = "scheduled" if it["status"] in ("waiting", "publishing") else it["status"]
        row["publish_account"] = it["account_nickname"] or it["account_id"]
        row["publish_time"] = it["plan_time"]
        row["publish_fail_reason"] = it["fail_reason"]
        return
    row["publish_state"] = "unpublished"


def delete_generated_video(video_id: str, keep_file: bool) -> None:
    """删除未占用成品（F-04-R15：组合键释放）。"""
    d = get_db()
    row = d.query_one("SELECT * FROM generated_video WHERE id=?", (video_id,))
    if not row:
        raise ValueError("成品不存在")
    if row["status"] == "occupied":
        raise ValueError("成品已被发布占用，需先取消对应发布明细")
    if not keep_file:
        (get_data_dir() / row["file_path"]).unlink(missing_ok=True)
    d.execute("DELETE FROM generated_video WHERE id=?", (video_id,))
