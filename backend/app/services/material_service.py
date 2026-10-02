# -*- coding: utf-8 -*-
"""素材库服务（F-03）：分类树、视频拉取任务、分享链接导入、文件上传、素材列表/维护。

入库三渠道统一管道：去重校验（视频ID / MD5）→ 文件落位 data/material → ffprobe 探测 → 入库。
"""

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, List, NamedTuple, Optional

from loguru import logger

from app.core import task_scheduler, notifier
from app.core.douyin import get_douyin_client, DouyinClientError, LoginInvalidError, RiskControlError, SearchBlockedError
from app.core.douyin.client import _derive_search_keyword
from app.core.ffmpeg import probe_media, extract_media_info
from app.db import get_db
from app.db.utils import now_str
from app.services.setting_service import get_data_dir, load_settings
from app.services import account_service
from app.services.task_service import task_service, raise_for_cancel, _fmt_hms, interruptible_sleep

_JOB_PREFIX = "video_pull:"
# #96：跨调用去重 set——同一 (pattern, error) 已打过 WARNING 就跳过，
# 防止 1000+ 视频每条都因同一条 re.error 刷屏
_RE_ERR_LOGGED: set[tuple[str, str]] = set()

# 固定"未分类"分类 ID（不落库，list API 注入虚拟节点；删除分类时素材 category_id 直接置为该值）
UNCATEGORIZED_ID = "-"

# 阶梯式翻页预算（每轮最多翻多少页 = max(MIN_PAGES, BASE - STEP * 已完成轮数)）。
# BASE 来自系统设置 pull_base_pages（默认 100，系统设置页可调），STEP/MIN 硬编码
# —— 改这两个风险大，开放给用户容易触发反爬/资源耗尽。
# 达 max_count 自动停用时不清零；用户点"重新启用"才重置 round 与 total_pulled。
_STEP_PULL_PAGES = 10
_MIN_PULL_PAGES = 10
# 基础页数下限保护：配置非法（≤0/None/字符串）时回退默认 100；过小（< MIN）也夹到 MIN。
# 防止用户把 BASE 调成 1 让阶梯失效。
_PULL_BASE_FLOOR = 10

# 内容检测抽帧失败子原因中文文案（inspect_reason → 上游日志文案）。
# 与 media_inspect.inspect_video 返回的 inspect_reason 枚举保持一一对应：
# - "no_video_stream": 图声视频 / m4a 伪装 mp4 / 抖音 play_addr 返回音频
# - "file_corrupted" : moov atom not found / Invalid data / metadata 不完整
# - "probe_error"    : 探测时长未知异常（极少触发）
# - "no_frames"      : 抽帧全失败（无可用帧，写盘失败等）
# 未命中以上枚举时回退默认文案「内容检测抽帧失败」。
_INSPECT_REASON_TEXT = {
    "no_video_stream": "源文件无视频流（图声视频/纯音频伪装）",
    "file_corrupted": "源文件损坏（ffprobe 失败）",
    "probe_error": "探测异常",
    "no_frames": "抽帧全失败（无可用帧）",
}


# B2: progress 模板单点 helper（避免分散定义导致 keepalive/回调/重试三处模板轮替）。
# 集中维护，文案调整只改一处。注：所有 helper 期望已格式化的秒数（不是 timestamp），
# 调用方负责 `int(time.time() - round_start)` 计算，helper 内部不再二次格式化。
class _ItemProgressCtx(NamedTuple):
    """阶段 B 单条进度上下文：收口 7 个参数便于 IDE 提示 + 未来扩展。

    字段语义见属性名；调用方按需构造（_run_detail_phase 闭包内）。
    """
    idx: int          # 当前序号（1-based）
    total: int        # 总条数
    aweme_id: str     # 视频 ID
    stage: str        # 当前阶段名（准备中 / 详情抓取中 / 下载入库中）
    new_count: int    # 已入库数（与 DB 字段同源）
    skip_count: int   # 跳过数（与 DB 字段同源）
    elapsed_s: int    # 已用秒数


def _progress_phase_a_idle(elapsed_s: int) -> str:
    """阶段 A keepalive 兜底模板：搜索阶段空闲中（无翻页回调时）。"""
    return f"搜索阶段：翻页采集中..., 用时 {_fmt_hms(elapsed_s)}"


def _progress_phase_a_scrolling(total_pages: int, total_videos: int, elapsed_s: int) -> str:
    """阶段 A 翻页回调模板：已抓到 N 页 / 累计 M 条。"""
    return (
        f"搜索阶段：已抓到 {total_pages} 页 / 累计 {total_videos} 条视频，"
        f"用时 {_fmt_hms(elapsed_s)}"
    )


def _progress_phase_a_retry(retry_n: int, remaining_s: int, elapsed_s: int) -> str:
    """阶段 A 重试等待模板：第 N 次重试等待中 / 剩 Xs。"""
    return (
        f"搜索阶段：第 {retry_n} 次重试等待中... 剩 {remaining_s}s，"
        f"用时 {_fmt_hms(elapsed_s)}"
    )


def _progress_phase_a_done(total_ids: int, elapsed_s: int) -> str:
    """阶段 A→B 切换模板：共抓到 N 个视频，准备进入下载阶段。"""
    return (
        f"搜索阶段完成：共抓到 {total_ids} 个视频，"
        f"准备进入下载阶段，用时 {_fmt_hms(elapsed_s)}"
    )


def _progress_phase_b(new_count: int, skip_count: int, elapsed_s: int) -> str:
    """阶段 B 拉取中模板：与 DB 字段同源（已入库 / 跳过）。"""
    return (
        f"拉取中：已入库 {new_count}，跳过 {skip_count}，用时 {_fmt_hms(elapsed_s)}"
    )


def _progress_phase_b_item(ctx: _ItemProgressCtx) -> str:
    """阶段 B 单条进度模板：当前 N/M 个 + 阶段名 + 已入库/跳过/用时。"""
    return (
        f"下载第 {ctx.idx}/{ctx.total} 个 (id={ctx.aweme_id}) {ctx.stage}，"
        f"已入库 {ctx.new_count}，跳过 {ctx.skip_count}，"
        f"用时 {_fmt_hms(ctx.elapsed_s)}"
    )


def _progress_terminal(total_pages: int, new_count: int, skip_count: int,
                       elapsed_s: int) -> str:
    """终态 progress 模板：总页数 / 新增 / 拦截 / 用时。"""
    return (
        f"总页数 {total_pages}，新增 {new_count}，"
        f"拦截 {skip_count}，用时 {_fmt_hms(elapsed_s)}"
    )


def _reject_and_cleanup(save_path: Path, video_id: str, reason: str,
                        sub_texts: Optional[List[str]] = None) -> str:
    """内容检测命中 / 抽帧失败时拒绝入库并清理：删文件 + 清空目录 + 日志。

    字幕分支会传 sub_texts（OCR 识别到的文本）便于排障日志，
    人脸分支不传。返回 "" 让上游 _download_video_candidate 知道该视频被过滤。
    """
    _cleanup_material_files(_safe_data_rel(save_path))
    if sub_texts:
        logger.info(
            "[过滤] 视频 {} {}，跳过入库 | 字幕内容: {}",
            video_id, reason, sub_texts,
        )
    else:
        logger.info("[过滤] 视频 {} {}，跳过入库", video_id, reason)
    return ""


def _get_pull_base_pages() -> int:
    """从系统设置读阶梯基础页数；非法值回退默认 100，过小夹到下限。"""
    from app.services.setting_service import load_settings
    raw = load_settings().get("pull_base_pages", 100)
    try:
        base = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(_PULL_BASE_FLOOR, base)



def compute_round_pages(pull_round: int) -> int:
    """按已完成轮数算本轮最大页数（线性递减，MIN 兜底，BASE 从设置读）。

    参数:
        pull_round: 已完成的轮数（0 = 即将跑第 1 轮；负数/None/字符串均按 0 容错）
    返回:
        本轮预算页数
    """
    # 负数回退到 0（避免 pages 超过 BASE 导致阶梯失效）
    round_no = max(0, int(pull_round or 0))
    pages = _get_pull_base_pages() - _STEP_PULL_PAGES * round_no
    return max(_MIN_PULL_PAGES, pages)


def migrate_empty_category_to_uncategorized(d) -> int:
    """启动时一次性迁移：历史 category_id='' 的素材 → UNCATEGORIZED_ID。

    旧数据中"未分类"用空串隐式表达，重构后统一为固定 ID '-'。
    返回迁移条数。
    """
    return d.execute(
        "UPDATE material SET category_id=? WHERE category_id='' AND deleted=0",
        (UNCATEGORIZED_ID,))

# 文件类型白名单（F-03-R6）
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MUSIC_EXTS = {".mp3", ".wav", ".aac", ".flac", ".m4a"}

# 发布/时长档位常量（F-03 拉取条件增强）
PUBLISH_RANGE_OPTIONS = {
    "any": None,        # 不限
    "1d": 1,            # 一天内
    "7d": 7,            # 一周内
    "180d": 180,        # 半年内
}
# 时长档位（毫秒）：max / min 为该侧阈值；max_eq / min_eq 决定是否包含边界
# 语义：lt1m=< 1 分钟（不含 1 分钟），1to5m=[1 分钟, 5 分钟]，gt5m=> 5 分钟（不含 5 分钟）
DURATION_RANGE_OPTIONS = {
    "any":    {"max": None, "min": None, "max_eq": True,  "min_eq": True},
    "lt1m":   {"max": 60_000,  "min": None, "max_eq": False, "min_eq": True},
    "1to5m":  {"max": 300_000, "min": 60_000,  "max_eq": True,  "min_eq": True},
    "gt5m":   {"max": None, "min": 300_000, "max_eq": True,  "min_eq": False},
}


# ---------- 分类树（F-03.1） ----------

def list_categories(material_type: str, orientation: str = "") -> list[dict]:
    """分类树列表（含素材计数），按 sort_order 排序返回平铺列表（前端组树）。

    "未分类"是虚拟节点（id='-'，不落库），始终在返回列表最前；count = 该 type 下 category_id='-' 素材数。

    #441：按 orientation（'vertical' / 'horizontal'）过滤每个分类的素材计数，
    与 list_materials 过滤一致（orientation=?）。orientation 空 = 不过滤。
    """
    d = get_db()
    # orientation 过滤子句：空 = 不过滤；非空 = orientation=?（与 list_materials 一致）
    ori_clause = " AND orientation=?" if orientation else ""
    ori_params = (orientation,) if orientation else ()
    rows = d.query_all(
        f"""SELECT c.*,
                   (SELECT COUNT(*) FROM material m WHERE m.category_id=c.id AND m.deleted=0{ori_clause}) AS direct_count
            FROM material_category c WHERE c.type=? AND c.deleted=0
            ORDER BY c.sort_order, c.create_time""",
        (*ori_params, material_type))
    # 计算含子孙的累计计数
    children: dict[str, list] = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(r)

    def subtree_count(node: dict) -> int:
        total = node["direct_count"]
        for ch in children.get(node["id"], []):
            total += subtree_count(ch)
        return total

    for r in rows:
        r["count"] = subtree_count(r)
    # 注入虚拟"未分类"节点（不存库）
    empty_count = d.query_one(
        f"SELECT COUNT(*) AS c FROM material WHERE category_id=? AND deleted=0 AND type=?{ori_clause}",
        (UNCATEGORIZED_ID, material_type, *ori_params))["c"]
    uncategorized = {
        "id": UNCATEGORIZED_ID,
        "parent_id": "",
        "name": "未分类",
        "type": material_type,
        "sort_order": -1,
        "direct_count": empty_count,
        "count": empty_count,
    }
    return [uncategorized] + rows


def create_category(name: str, material_type: str, parent_id: str = "") -> dict:
    """新增分类（层级上限 4 级；不允许在系统分类「未分类」下创建子分类）。"""
    d = get_db()
    if material_type not in ("video", "music"):
        raise ValueError("类型非法")
    if parent_id == UNCATEGORIZED_ID:
        raise ValueError("系统分类「未分类」下不可新增子分类")
    depth = 0
    pid = parent_id
    while pid:
        row = d.query_one("SELECT parent_id FROM material_category WHERE id=? AND deleted=0", (pid,))
        if not row:
            raise ValueError("父分类不存在")
        pid = row["parent_id"]
        depth += 1
        if depth >= 4:
            raise ValueError("分类层级上限 4 级")
    # 同 parent 下取当前 max(sort_order) + 1；"未分类"为虚拟节点不占位
    order_row = d.query_one(
        """SELECT COALESCE(MAX(sort_order), 0) AS m FROM material_category
           WHERE parent_id=? AND deleted=0""",
        (parent_id,))
    order = (order_row["m"] if order_row else 0) + 1
    cat_id = d.insert("material_category", {
        "name": name, "type": material_type, "parent_id": parent_id, "sort_order": order})
    return d.query_one("SELECT * FROM material_category WHERE id=?", (cat_id,))


def rename_category(category_id: str, name: str) -> None:
    """重命名分类。"""
    if category_id == UNCATEGORIZED_ID:
        raise ValueError("系统分类「未分类」不可重命名")
    get_db().update_by_id("material_category", category_id, {"name": name})


def move_category(category_id: str, new_parent_id: str, sort_order: int = 0) -> None:
    """移动分类（换父级/排序）；不允许把自己移到自己子孙下。"""
    if category_id == UNCATEGORIZED_ID:
        raise ValueError("系统分类「未分类」不可移动")
    # 「未分类」是虚拟节点（无 DB 记录），不能作为其他分类的父级
    if new_parent_id == UNCATEGORIZED_ID:
        raise ValueError("「未分类」下不可放置子分类")
    d = get_db()
    if new_parent_id == category_id:
        raise ValueError("不能移动到自身下")
    # 祖先环检测
    pid = new_parent_id
    while pid:
        if pid == category_id:
            raise ValueError("不能移动到自己的子孙分类下")
        row = d.query_one("SELECT parent_id FROM material_category WHERE id=?", (pid,))
        pid = row["parent_id"] if row else ""
    d.update_by_id("material_category", category_id, {"parent_id": new_parent_id, "sort_order": sort_order})


def delete_category(category_id: str, strategy: str = "to_parent") -> None:
    """删除分类（需无子分类；素材按策略处置：to_parent 移入父分类 / must_empty 强制清空）。"""
    if category_id == UNCATEGORIZED_ID:
        raise ValueError("系统分类「未分类」不可删除")
    d = get_db()
    row = d.query_one("SELECT * FROM material_category WHERE id=? AND deleted=0", (category_id,))
    if not row:
        raise ValueError("分类不存在")
    if d.query_one("SELECT COUNT(*) AS c FROM material_category WHERE parent_id=? AND deleted=0",
                   (category_id,))["c"] > 0:
        raise ValueError("请先删除或移出子分类")
    material_count = d.query_one(
        "SELECT COUNT(*) AS c FROM material WHERE category_id=? AND deleted=0", (category_id,))["c"]
    if material_count > 0:
        if strategy != "to_parent":
            raise ValueError(f"分类下有 {material_count} 个素材，请先迁移")
        # 顶级分类（parent_id 为空）时素材迁入"未分类"虚拟分类（id='-'）；其他迁入父分类
        new_cat = row["parent_id"] or UNCATEGORIZED_ID
        d.execute("UPDATE material SET category_id=? WHERE category_id=?", (new_cat, category_id))
    # 根节点不可删（parent_id 空串为类型根占位，实际不建根记录，此处防御）
    d.soft_delete_by_id("material_category", category_id)


# ---------- 统一入库管道 ----------

def _build_material_path(material_type: str, category_id: str, material_id: str, title: str, ext: str) -> Path:
    """按目录规范生成存储路径（#364）：material/{type}/{yyyyMMdd}/{id}/{id}.{ext}。

    每个素材独占一层 id/ 子目录，下面放视频 / 封面 / 头像三个文件：
        material/<type>/<date>/<id>/<id>.<ext>            —— 视频/音频本体
        material/<type>/<date>/<id>/<id>_cover.<ext>      —— 封面
        material/<type>/<date>/<id>/<id>_avatar.<ext>     —— 头像

    不带分类层级（分类与目录解耦，改分类无需搬文件）；文件名只用素材 ID（标题改存 DB，
    避免特殊字符/超长截断问题）。title 参数保留以兼容既有调用签名，实际不参与路径。
    """
    date = now_str()[:10].replace("-", "")
    rel = Path(material_type) / date / material_id / f"{material_id}{ext}"
    return get_data_dir() / "material" / rel


def _build_material_cover_path(material_type: str, material_id: str, ext: str,
                               date: str | None = None) -> str:
    """#364：素材封面相对路径。`date` 留空用今日；指定时用于历史素材补缓存。"""
    d = (date or now_str()[:10]).replace("-", "")
    return f"material/{material_type}/{d}/{material_id}/{material_id}_cover{ext}"


def _build_material_avatar_path(material_type: str, material_id: str, ext: str,
                                date: str | None = None) -> str:
    """#364：素材头像相对路径。"""
    d = (date or now_str()[:10]).replace("-", "")
    return f"material/{material_type}/{d}/{material_id}/{material_id}_avatar{ext}"


def _md5_of_file(path: Path) -> str:
    """文件内容 MD5。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_empty_dir(parent: Path, remove_files: bool = False) -> None:
    """清理空目录：mkdir 创建后若下载/入库失败，目录可能留下空壳。
    用 rmdir 尝试删除（仅空目录可删，OSError 自动忽略）；
    不递归向上，避免误删父目录（如 20260930/ 仍有其他视频）。

    参数:
        remove_files: True 时先 best-effort 删目录内所有文件再 rmdir。
            用于 MD5 重场景 — video.mp4 unlink 后 cover/avatar 还在同目录，
            默认行为会因 ENOTEMPTY 跳过，cover/avatar 成孤儿。
            调用方传 True 确保整目录一起清，避免孤儿。
    """
    try:
        parent.rmdir()
        return
    except OSError:
        if not remove_files:
            return
    # remove_files=True + 非空 → 清空后重试 rmdir（cover/avatar 等孤儿）
    try:
        for child in parent.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        parent.rmdir()
    except OSError:
        # 并发覆盖后仍非空 / 权限不足，跳过
        pass


# 模块级缓存 data_dir.resolve() 结果（避免每次 cleanup 都 stat 一次文件系统）
_DATA_ROOT_RESOLVED: Path | None = None


def _resolve_data_root() -> Path:
    """data_dir resolve 单例（防 1000+ 视频每条调一次 stat）。"""
    global _DATA_ROOT_RESOLVED
    if _DATA_ROOT_RESOLVED is None:
        _DATA_ROOT_RESOLVED = get_data_dir().resolve()
    return _DATA_ROOT_RESOLVED


def _invalidate_data_root_cache() -> None:
    """失效 _DATA_ROOT_RESOLVED 缓存。

    TODO（#592 follow-up）：settings_service 切换 data_dir 时必须调一次本函数，
    否则旧路径仍被缓存使用，所有 cleanup 静默失效。当前 settings_service
    未暴露 data_dir 热切接口，本函数预留 hook。
    """
    global _DATA_ROOT_RESOLVED
    _DATA_ROOT_RESOLVED = None


# Windows 8.3 短名格式：1-6 字符 + `~` + 1-3 位数字 + 可选扩展名（PROGRA~1、ARCHIV~2.MP4）
# 整段匹配（前后是路径分隔符或边界），不误判合法目录 `archive~1`
_SHORT_NAME_RE = re.compile(
    r"(?i)(?:^|[\\/])[A-Z0-9]{1,6}~\d{1,3}(?:\.[A-Z0-9]{1,3})?(?:[\\/]|$)"
)


def _resolve_safe(p: Path) -> Path | None:
    """p → 解析后的绝对路径；不安全返 None（resolve 失败 / UNCUTE `\\?\\` / 8.3 短名 / 路径外）。

    `_is_within_data_root` 和 `_safe_data_rel` 共用此解析结果，避免重复 resolve + is_relative_to。
    """
    try:
        p_abs = p.resolve()
    except OSError:
        return None
    s = str(p_abs)
    if "\\?\\" in s or _SHORT_NAME_RE.search(s):
        return None
    try:
        if not p_abs.is_relative_to(_resolve_data_root()):
            return None
    except OSError:
        return None
    return p_abs


def _is_within_data_root(p: Path) -> bool:
    """校验 p 解析后是否仍在 data_root 内（防路径穿越 + Windows 8.3 / UNC）。"""
    return _resolve_safe(p) is not None


def _cleanup_material_files(file_rel: str | None,
                            cover_rel: str | None = None,
                            avatar_rel: str | None = None) -> None:
    r"""统一清理素材物理文件（任务 #592：撤销入库 / 删除素材复用）。

    删除 file_rel（视频本体）+ cover_rel（封面）+ avatar_rel（头像），
    然后 best-effort 清空 file_rel 的父目录（force 一级清理）。
    入参路径都是相对 data 根的字符串（material.file_path / cover_url / author_avatar）；
    任何字段为空（None/''/目录不存在）跳过；删不到的文件静默吞 OSError。

    安全：所有 rel 通过 `_resolve_safe` 校验（防 caller 传『../../../xxx』路径穿越
    + UNC `\\?\` 设备路径 + Windows 8.3 短名 + symlink 跳出 data_root）。
    校验失败跳过并 WARNING 日志。
    """
    data_dir = get_data_dir()
    rels = [r for r in (file_rel, cover_rel, avatar_rel) if r]
    for rel in rels:
        p = _resolve_safe(data_dir / rel)
        if p is None:
            logger.warning(
                "[清理] rel 校验失败（路径穿越 / UNC / 8.3 短名），跳过删除 rel={}",
                rel,
            )
            continue
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass
    if file_rel:
        parent_abs = _resolve_safe(data_dir / file_rel)
        if parent_abs is not None:
            _cleanup_empty_dir(parent_abs.parent, remove_files=True)
        else:
            logger.warning("[清理] 父目录校验失败，跳过目录清理 rel={}", file_rel)


def _safe_data_rel(p: Path) -> str | None:
    """Path → 相对 data_dir 字符串（正斜杠）；不在内返 None。

    与 _resolve_safe 同一套校验（UNC `\\?\\` / 8.3 短名 / 路径外 → None）。
    用于调用 _cleanup_material_files 时统一计算 file_rel，避免重复 try/except。
    复用 _resolve_safe 解析（不重复 resolve + is_relative_to）；relative_to
    不会抛 ValueError（_resolve_safe 已 is_relative_to 校验通过）。
    """
    p_abs = _resolve_safe(p)
    if p_abs is None:
        return None
    return str(p_abs.relative_to(_resolve_data_root())).replace("\\", "/")


def _transcode_video_to_mp4(src_path: Path) -> Path | None:
    """任意视频统一转 mp4（#592）。

    - 视频流优先 `-c:v copy`（无重编开销）
    - 有 audio 轨 → `-c:a aac` 重编音频（保证容器内 aac 统一）
    - 无 audio 轨 → 加 anullsrc stereo44100 静音轨（#592 补全元数据要求）
    - 产物 = src_path 同目录 `<stem>_transcoded.mp4`
    - 失败返 None（产物文件若已部分生成会被 unlink）
    """
    from app.core.ffmpeg import FFMPEG, run_cmd, probe_media
    info = probe_media(str(src_path))
    has_audio = False
    if info:
        for s in info.get("streams", []) or []:
            if s.get("codec_type") == "audio":
                has_audio = True
                break
    out_path = src_path.with_name(src_path.stem + "_transcoded.mp4")
    if has_audio:
        cmd = [
            FFMPEG, "-y", "-i", str(src_path),
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        cmd = [
            FFMPEG, "-y", "-i", str(src_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            "-map", "0:v:0", "-map", "1:a:0", "-movflags", "+faststart",
            str(out_path),
        ]
    result = run_cmd(cmd)
    if result is None or result.returncode != 0 or not out_path.exists():
        out_path.unlink(missing_ok=True)
        tail = ""
        if result is not None and result.stderr:
            tail_lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()][-3:]
            tail = "\n".join(tail_lines)
        logger.warning("[转码] 视频→mp4 失败 src={} has_audio={} tail=\n{}",
                       src_path.name, has_audio, tail)
        return None
    return out_path


def _transcode_audio_to_mp3(src_path: Path) -> Path | None:
    """任意音频统一转 mp3（#592，libmp3lame q=2）。"""
    from app.core.ffmpeg import FFMPEG, run_cmd
    out_path = src_path.with_name(src_path.stem + "_transcoded.mp3")
    cmd = [
        FFMPEG, "-y", "-i", str(src_path),
        "-c:a", "libmp3lame", "-q:a", "2",
        str(out_path),
    ]
    result = run_cmd(cmd)
    if result is None or result.returncode != 0 or not out_path.exists():
        out_path.unlink(missing_ok=True)
        tail = ""
        if result is not None and result.stderr:
            tail_lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()][-3:]
            tail = "\n".join(tail_lines)
        logger.warning("[转码] 音频→mp3 失败 src={} tail=\n{}", src_path.name, tail)
        return None
    return out_path


def _infer_image_ext(url: str, default: str = ".webp") -> str:
    """#364：从 URL 推断图片后缀（取查询参数前的路径段），无则回退默认。"""
    if not url:
        return default
    path_part = url.split("?", 1)[0]
    ext = Path(path_part).suffix.lower()
    if ext and ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return ext
    return default


def _probe_and_save(material_id: str, title: str, material_type: str, category_id: str,
                    file_path: Path, source_type: str, source_ref: str | None,
                    raw_link: str | None, md5: str) -> dict:
    """落位文件 + ffprobe 探测 + 入库（供三渠道共用）。"""
    d = get_db()
    duration_ms = None
    resolution = None
    orientation = None
    if material_type == "video":
        info = extract_media_info(probe_media(str(file_path)))
        duration_ms = info["duration_ms"] or None
        if info["width"] and info["height"]:
            resolution = f"{info['width']}x{info['height']}"
        orientation = info["orientation"]
    elif material_type == "music":
        # 音频也探时长（ffprobe 对 mp3/m4a 通用；失败保持 None，详情显示 -）
        info = extract_media_info(probe_media(str(file_path)))
        duration_ms = info["duration_ms"] or None
    rel_path = str(file_path.relative_to(get_data_dir())).replace("\\", "/")
    d.insert("material", {
        "id": material_id,
        "title": title,
        "author_title": title,
        "type": material_type,
        "category_id": category_id,
        "duration_ms": duration_ms,
        "file_size": file_path.stat().st_size,
        "source_type": source_type,
        "source_ref": source_ref,
        "file_md5": md5,
        "resolution": resolution,
        "orientation": orientation,
        "file_path": rel_path,
        "raw_link": raw_link,
    })
    return d.query_one("SELECT * FROM material WHERE id=?", (material_id,))


def _duplicate_check(source_ref: str | None, md5: str) -> dict | None:
    """去重：拉取/分享按来源视频 ID，上传按 MD5；返回已存在素材或 None。"""
    d = get_db()
    if source_ref:
        row = d.query_one("SELECT * FROM material WHERE source_ref=? AND deleted=0", (source_ref,))
        if row:
            return row
    return d.query_one("SELECT * FROM material WHERE file_md5=? AND deleted=0", (md5,))


# ---------- 视频拉取任务（F-03.2 / F-03.7） ----------

def create_pull_task(task_name: str, conditions: dict, account_id: str,
                     category_id: str, interval_config: dict) -> dict:
    """创建视频拉取任务（定时）。"""
    interval_json = json.dumps(interval_config, ensure_ascii=False)
    task_scheduler.parse_interval_config(interval_json)
    d = get_db()
    if not d.query_one("SELECT id FROM account WHERE id=? AND deleted=0", (account_id,)):
        raise ValueError("拉取账号不存在")
    # 允许 UNCATEGORIZED_ID 作为入库目标（虚拟分类）；其他必须存在且为视频类型
    if category_id != UNCATEGORIZED_ID and not d.query_one(
            "SELECT id FROM material_category WHERE id=? AND type='video' AND deleted=0",
            (category_id,)):
        raise ValueError("入库分类必须为视频类型分类")
    task_id = d.insert("video_pull_task", {
        "task_name": task_name,
        "conditions_json": json.dumps(conditions, ensure_ascii=False),
        "account_id": account_id,
        "category_id": category_id,
        "interval_config": interval_json,
        "status": "enabled",
    })
    d.update_by_id("video_pull_task", task_id, {
        "next_run_time": task_scheduler.compute_next_run_time(interval_json)})
    register_task_job(d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,)))
    return d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,))


def update_pull_task(task_id: str, task_name: str | None, conditions: dict | None,
                     account_id: str | None, category_id: str | None,
                     interval_config: dict | None) -> dict:
    """编辑视频拉取任务（下一轮生效）。"""
    d = get_db()
    if not d.query_one("SELECT id FROM video_pull_task WHERE id=? AND deleted=0", (task_id,)):
        raise ValueError("任务不存在")
    fields: dict = {}
    if task_name is not None:
        fields["task_name"] = task_name
    if conditions is not None:
        fields["conditions_json"] = json.dumps(conditions, ensure_ascii=False)
    if account_id is not None:
        fields["account_id"] = account_id
    if category_id is not None:
        fields["category_id"] = category_id
    if interval_config is not None:
        interval_json = json.dumps(interval_config, ensure_ascii=False)
        task_scheduler.parse_interval_config(interval_json)
        fields["interval_config"] = interval_json
    d.update_by_id("video_pull_task", task_id, fields)
    updated = d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,))
    if updated["status"] == "enabled":
        register_task_job(updated)
    return updated


def toggle_pull_task(task_id: str, enabled: bool) -> dict:
    """视频拉取任务启停（保留阶梯进度，不重置 round / total_pulled）。

    v23 联动 stop_reason：手动停用 → 'manual'；手动启用 → 清回 null。
    自动达 max_count 停用时由 _run_pull_round 写 'auto_max'，toggle 不覆盖，
    保证"重新启用"按钮只在自动停用下出现。

    阶梯重置语义：达 max_count 自动停用 → 用户主动点"重新启用"按钮（调
    restart_pull_task）才清零 round 与 total_pulled；普通 toggle 不清零，
    阶梯进度跨启用/停用保留。
    """
    d = get_db()
    fields: dict = {"status": "enabled" if enabled else "disabled"}
    if enabled:
        # 手动启用：清掉 'manual'/'auto_max' 标记（auto_max 在 restart 路径会被 restart_pull_task 清）
        fields["stop_reason"] = None
        d.update_by_id("video_pull_task", task_id, fields)
        register_task_job(d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,)))
    else:
        # 手动停用：标记 manual（不覆盖 auto_max）
        cur = d.query_one("SELECT stop_reason FROM video_pull_task WHERE id=?", (task_id,))
        if cur and cur.get("stop_reason") != "auto_max":
            fields["stop_reason"] = "manual"
        d.update_by_id("video_pull_task", task_id, fields)
        task_scheduler.remove_job(_JOB_PREFIX + task_id)
        d.update_by_id("video_pull_task", task_id, {"next_run_time": None})
    return d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,))


def restart_pull_task(task_id: str) -> dict:
    """v22 阶梯重置 + v23 标记清理：清零 pull_round + total_pulled + stop_reason，
    重新启用任务。

    用途：达 max_count 自动停用后，前端任务列表操作列展示"重新启用"按钮，
    用户点击调此端点。下次调度按第 1 轮 100 页跑起。

    边界：仅对 status='disabled' 生效（避免误清零正在跑的进度）；
    启用前重新计算 next_run_time 并注册调度。
    """
    d = get_db()
    task = d.query_one("SELECT * FROM video_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        raise ValueError("任务不存在")
    if task["status"] != "disabled":
        raise ValueError(f"任务当前状态为 {task['status']}，仅停用任务可重新启用")
    d.update_by_id("video_pull_task", task_id, {
        "status": "enabled",
        "pull_round": 0,
        "total_pulled": 0,
        "stop_reason": None,
        "next_run_time": task_scheduler.compute_next_run_time(task["interval_config"]),
    })
    register_task_job(d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,)))
    return d.query_one("SELECT * FROM video_pull_task WHERE id=?", (task_id,))


def delete_pull_task(task_id: str) -> None:
    """删除视频拉取任务（已入库素材保留）。

    任务 #133：删除前先取消 in-flight worker。读 bg_task_id（_run_pull_round 启
    动时写入的 task_service UUID），调 request_cancel 让 worker 在下一次 page
    边界 raise_for_cancel 处退出循环，避免已飞轮次继续入库。
    """
    d = get_db()
    if not d.query_one("SELECT id FROM video_pull_task WHERE id=? AND deleted=0", (task_id,)):
        raise ValueError("任务不存在")
    # 1) 取消 in-flight worker（如果有）。request_cancel 对已完成/不存在任务返 False，幂等。
    bg_task_id = d.query_one(
        "SELECT bg_task_id FROM video_pull_task WHERE id=?", (task_id,))
    if bg_task_id and bg_task_id.get("bg_task_id"):
        try:
            cancelled = task_service.request_cancel(bg_task_id["bg_task_id"])
            logger.info(
                "[拉取任务] 删除时取消 in-flight worker task_id={} bg_task_id={} ok={}",
                task_id, bg_task_id["bg_task_id"], cancelled,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("[拉取任务] 取消 bg_task_id 异常：{}", e)
    # 2) 移除 APScheduler 注册
    task_scheduler.remove_job(_JOB_PREFIX + task_id)
    # 3) 软删除配置 + 清日志
    d.soft_delete_by_id("video_pull_task", task_id)
    d.execute("DELETE FROM video_pull_log WHERE task_id=?", (task_id,))


def list_pull_tasks(page: int = 1, page_size: int = 20) -> dict:
    """视频拉取任务列表。

    每行附加 current_run 字段（来自 task_service 内存实例），便于任务队列
    列表展示正在执行的进度（bg_task_id 命中 running/waiting 时填 status/
    progress/message，否则 null）。
    """
    from app.services.task_service import task_service
    d = get_db()
    page_result = d.query_page(
        """SELECT t.*, a.nickname AS account_nickname, c.name AS category_name
           FROM video_pull_task t
           LEFT JOIN account a ON t.account_id=a.id
           LEFT JOIN material_category c ON t.category_id=c.id
           WHERE t.deleted=0 ORDER BY t.create_time DESC""",
        (), page, page_size)
    # 注入当前执行进度（bg_task_id → task_service 内存实例）
    for row in page_result.get("list", []):
        bg_id = row.get("bg_task_id")
        run_info = task_service.get_task(bg_id) if bg_id else None
        if run_info and run_info.get("status") in ("waiting", "running"):
            row["current_run"] = {
                "status": run_info["status"],
                "progress": run_info.get("progress", ""),
                "message": run_info.get("message", ""),
            }
        else:
            row["current_run"] = None
    return page_result


def delete_pull_log(log_id: str) -> None:
    """删除单条视频拉取执行日志（#351：聚合列表批量删除）。"""
    d = get_db()
    row = d.query_one("SELECT id FROM video_pull_log WHERE id=?", (log_id,))
    if not row:
        raise ValueError("日志不存在")
    d.execute("DELETE FROM video_pull_log WHERE id=?", (log_id,))


def list_pull_logs(task_id: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """视频拉取任务执行日志列表。

    task_id 为空时聚合所有任务的执行日志（按时间倒序，含任务名）。
    """
    if task_id:
        return get_db().query_page(
            """SELECT id, run_time, new_count, skip_count, fail_reason
               FROM video_pull_log
               WHERE task_id=? ORDER BY run_time DESC""",
            (task_id,), page, page_size)
    return get_db().query_page(
        """SELECT l.id, l.task_id, l.run_time, l.new_count, l.skip_count, l.fail_reason,
                  t.task_name
           FROM video_pull_log l
           LEFT JOIN video_pull_task t ON l.task_id=t.id
           ORDER BY l.run_time DESC""",
        (), page, page_size)


def register_task_job(task_row: dict) -> None:
    """注册调度（main.py 启动恢复调用同名函数）。"""
    def _on_fire(tid=task_row["id"]):
        # 任务 #133：把 task_service 提交的 UUID 写回 task 行，删除时可取消。
        # PR2 #55：type 从 pull 拆为 video_pull。
        # 任务 #59 P1 #4：每次 fire 重新查 task_name（闭包快照会被旧值锁住，编辑任务名后无效）
        row = get_db().query_one("SELECT task_name FROM video_pull_task WHERE id=?", (tid,))
        tname = row["task_name"] if row else "未命名任务"
        bg_task_id = task_service.submit(
            "video_pull", tname, lambda info: _run_pull_round(tid, info))
        try:
            get_db().execute(
                "UPDATE video_pull_task SET bg_task_id=? WHERE id=?",
                (bg_task_id, tid))
        except Exception as e:  # noqa: BLE001
            logger.debug("[拉取任务] 写 bg_task_id 失败 {}：{}", tid, e)
    task_scheduler.register_interval_job(
        _JOB_PREFIX + task_row["id"], task_row["interval_config"], _on_fire)


def _run_pull_round(task_id: str, info) -> str:
    """执行一轮视频拉取：搜索 → 逐条条件校验 → 下载入库。

    单轮入库数量达到 `conditions.max_count`（默认 100，上限 1000）后自动停用任务：
    - 调度器移除该任务的下一次执行（remove_job）
    - 数据库 status 置为 disabled / next_run_time 清空
    - 用户可手动重新启用并按需调整 max_count

    日志规范（v126 增强）：
    - 拦截跳过：标题/描述/作者 + 首个拦截原因
    - 重复跳过：已存在素材标题
    - 入库成功：素材 ID + 标题/作者
    """
    d = get_db()
    task = d.query_one("SELECT * FROM video_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        return "任务已删除，跳过"
    task_scheduler.touch_last_run("video_pull_task", task_id)
    conditions = json.loads(task["conditions_json"])
    # #95：摘要过滤条件 INFO（含 title_regex pattern），让用户/运维一眼看出
    # 实际生效的过滤规则——之前正则配错 _match_conditions 静默 pass，
    # 一轮跑完才发现"为什么所有 title_regex 都没生效"。
    # 通俗日志：只输出非默认条件。
    filters = []
    if conditions.get("title_regex"):
        filters.append(f"标题包含「{conditions['title_regex']}」")
    if conditions.get("orientation"):
        filters.append(f"画面方向={conditions['orientation']}")
    if conditions.get("publish_range") and conditions["publish_range"] != "any":
        filters.append(f"发布时间={conditions['publish_range']}")
    if conditions.get("duration_range") and conditions["duration_range"] != "any":
        filters.append(f"视频时长={conditions['duration_range']}")
    if filters:
        logger.info("[拉取] 任务「{}」应用过滤：{}", task["task_name"], "，".join(filters))
    else:
        logger.info("[拉取] 任务「{}」无过滤条件，全量拉取", task["task_name"])
    # max_count 边界：Pydantic 已限制 1-1000；防御性 clamp
    try:
        max_count = int(conditions.get("max_count") or 100)
    except (TypeError, ValueError):
        max_count = 100
    max_count = max(1, min(1000, max_count))
    account = d.query_one("SELECT * FROM account WHERE id=? AND deleted=0", (task["account_id"],))
    account_id=account["id"]  # 缓存供阶段 B 详情抓取走 storage_state
    if not account or account["status"] in ("invalid", "disabled"):
        _write_pull_log(task_id, 0, 0, "账号登录态失效，跳过本轮")
        return "账号登录态失效，跳过本轮"

    client = get_douyin_client()
    cookie = account_service.get_cookie(account["id"])
    # #506：素材搜索改走浏览器持久化会话（和发布视频一致），
    # 旧版 curl_cffi 裸 cookie 被搜索接口 2483 风控。
    # #P0-9：search_session 仅用于当页 search_videos，处理详情前必须 close——
    # Playwright sync_api 一个线程只能有一个 running loop，search_session 的
    # dispatcher loop 在跑时 BrowserActor._ensure_browser 调第二次 sync_playwright
    # 会抛 "inside the asyncio loop"。每页 close+重建避免持久化登录态丢失
    # （profile 持久化由 launch_persistent_context 负责，重建只是订阅登录上下文，不丢登录）。
    from app.core.douyin.search_api import BrowserSearchSession
    from app.services.douyin_account import get_profile_dir
    profile_dir = get_profile_dir(account["id"])
    # #审查决定：素材拉取硬编码 headless=True（不显示浏览器窗口）。
    # 原因：拉取任务频率高、profile_dir 短时间内被 BrowserSearchSession 和 BrowserActor
    # 串行使用，headless=False 时两个 chromium 实例的 lock file 冲突（实测浏览器
    # 已关闭类异常）。需要观察浏览器内部行为（搜索/详情/补抓身份）走 publish / check /
    # 登录窗路径，这三类跟 browser_show_window 配置切换 headless。
    new_count = skip_count = 0
    fail_reason = ""
    reached_limit = False
    # 任务 #66：统一用 task_service 注入的 start_ts（替代原 round_start，
    # 这样阶梯多轮翻页时 progress 用时 = 整个 worker 周期，而非单轮）
    round_start = info.start_ts or time.time()
    # v22 阶梯式翻页：按已完成轮数算本轮预算页数（第 1 轮 100、第 2 轮 90 ... 10 兜底）。
    # 用户决策：达 max_count 自动停用不清零；手动"重新启用"按钮才重置 round + total_pulled。
    pull_round = int(task.get("pull_round") or 0)
    page_budget = compute_round_pages(pull_round)
    # 507 改造：阶段 A 需要直接拿 keyword 给 search_all_for_ids。原版只在
    # client.search_videos 内部用 _derive_search_keyword 派生，worker 不持有。
    # 这里派生一次，阶段 A + 早退重试共用。
    keyword = _derive_search_keyword(conditions)
    # 阶段 B 实际处理视频计数（外层作用域，供 finally 后的日志使用）。
    # 507 改造后已无逐页 page 计数（搜索阶段一次拿完 + 阶段 B 按 aweme_id 循环），
    # 日志「拉取完成 第 X 页」改用阶段 B 处理数代替。
    detail_processed = 0

    def _write_progress() -> None:
        """统一写入 progress 模板（多处调用：入循环前 / 阶段 B 完成 / 4 个 except 块）。

        阶段感知（与 _keepalive_loop 配合）：
        - 阶段 A（搜索）：keepalive 每 10s 调一次，写「翻页采集中...」模板
          （覆盖 _on_scroll_progress 写入的「已抓到 N 页」详细模板——这是设计
          意图：让用户在翻页慢或无 XHR 时感知「采集中」）。
        - 阶段 B（下载）：keepalive 不调（避免覆盖 _fmt_progress 写入的
          「下载第 X/N 个 详情抓取中」详细模板）。本函数在阶段 B 仅作兜底
          ——progress 为空时（异常路径）初始化拉取中模板，确保前端有显示。
        错误细节走 info.message（不在 progress 里混错误类型，避免模板撕裂）。

        拦截计数用 skip_count（与 _write_pull_log 写入 DB 的字段一致）：
        - skip_count 是超集（filtered + duplicate + failed 三类拒绝），与 DB 字段同源
        - 进度显示与 DB 记录必须同源，避免语义错位
        """
        elapsed = int(time.time() - round_start)
        if _search_phase_active.is_set():
            # 阶段 A：写入「翻页采集中」模板（含已用时）
            info.progress = _progress_phase_a_idle(elapsed)
        # 阶段 B：不覆盖 _fmt_progress 写入的详细模板
        # 兜底：info.progress 为空时写入拉取中模板（防止前端显示空 progress）
        elif not info.progress:
            info.progress = _progress_phase_b(new_count, skip_count, elapsed)

    # 阶段标记：_write_progress 感知当前阶段切换模板。
    # 阶段 A（搜索）期间 keepalive 写入搜索模板（不覆盖搜索回调的更详细进度），
    # 阶段 B（下载）期间写入拉取中模板（已入库 X / 跳过 Y）。
    # 必须先定义再 _write_progress()，否则闭包延迟绑定会抛
    # "cannot access free variable '_search_phase_active' where it is not
    # associated with a value in enclosing scope"。
    _search_phase_active = threading.Event()
    _search_phase_active.set()  # 默认搜索阶段（_run_pull_round 入口）

    # 入循环前先写一次初始 progress——首轮 search_videos 期间（约 5-20s，
    # 含 XHR + 风控握手）状态栏才能看到「拉取第 1/N 页」而不是空白，
    # 空页早退（empty_pages/has_more=0）时也有初始值兜底。
    _write_progress()

    # #审查建议：60s search_videos 阻塞期间主线程被卡住，progress 字面静止
    # 用户在状态栏看不到「用时」数字递增会误判卡死。
    # keepalive daemon 每 10s 调一次 _write_progress()，仅刷新「用时」字段，
    # 让用户感知任务还活着。worker 退出时通过 _keepalive_stop 通知停止。
    # 阶段 B 时不调 _write_progress（避免覆盖 _fmt_progress 写入的
    # 「下载第 X/N 个 详情抓取中」详细模板，让用户看到当前在做什么）。
    _keepalive_stop = threading.Event()
    def _keepalive_loop() -> None:
        while not _keepalive_stop.is_set():
            # wait 带超时，可被 set() 立即唤醒
            if _keepalive_stop.wait(10):
                break
            try:
                # 阶段 A 调 _write_progress 刷新用时；
                # 阶段 B 不调（_fmt_progress 每次循环都更新 progress 含已用时）
                if _search_phase_active.is_set():
                    _write_progress()
            except Exception as exc:  # noqa: BLE001
                # daemon 异常属于「状态栏停止刷新」的用户体验降级场景，用 warning 留痕
                # （debug 默认不输出，等于没留痕）。daemon 在 worker 退出但 stop 尚未生效的
                # 窗口期可能触发异常，不抛出避免日志污染。
                logger.warning("keepalive tick 异常 {}", exc)
    _keepalive_thread = threading.Thread(target=_keepalive_loop, daemon=True, name="pull-progress-keepalive")
    _keepalive_thread.start()

    # 改造 #507：阶段 A + 阶段 B 分离编排
    # 阶段 A：搜索 + 筛选面板 UI 化 + wheel 翻页一次拿完所有 aweme_id
    # 阶段 B：顺序循环 aweme_ids → BrowserActor 详情 + 下载 + 入库
    # 两阶段资源隔离：阶段 A 关闭 search_session 后才进阶段 B，避免 asyncio loop 冲突。
    search_session = None
    aweme_ids: list[str] = []
    try:
        # ==================== 阶段 A：搜索 + 筛选 + 翻页 ====================
        # 复用 search_all_for_ids（v5 wheel + 筛选面板 UI 化），不再逐页遍历。
        # _searched_keyword 由 BrowserSearchSession 内部维护：同 keyword 跨任务复用。
        search_session = BrowserSearchSession(profile_dir, headless=None)
        # 进度模板：搜索阶段（B2: 单点 helper 维护）
        info.progress = _progress_phase_a_idle(int(time.time() - round_start))
        # A4: 阶段 A 启动日志改 DEBUG。用户已从 progress「搜索阶段：翻页采集中...」
        # + task_service 状态感知任务在跑，避免「开始搜索」INFO 与 search_api 内部
        # 「[搜索] 启动浏览器」+「已发起搜索」三连刷屏。debug 留 trace 供排障。
        logger.debug("[拉取] 任务「{}」阶段 A 启动关键词={}", task["task_name"], keyword)

        # 任务 #589：实时翻页进度回调，每抓到一页 XHR 调一次刷新 info.progress
        def _on_scroll_progress(total_pages: int, total_videos: int) -> None:
            info.progress = _progress_phase_a_scrolling(
                total_pages, total_videos, int(time.time() - round_start),
            )

        aweme_ids = search_session.search_all_for_ids(
            keyword=keyword, conditions=conditions,
            idle_timeout=60, max_pages=page_budget,
            debug=load_settings().get("pull_debug", False),
            progress_cb=_on_scroll_progress,
        )
        _write_progress()
        if not aweme_ids:
            # 507 改造：搜索阶段 0 数据 → 早退重试整轮搜索
            # 重试 2 次（30s/45s），仍无数据 → fail_reason 走 B
            logger.info("[拉取] 没搜到任何视频，准备重试")
            retried_with_data = False
            for retry in range(2):
                wait_sec = 30 + retry * 15
                logger.warning(
                    "[拉取] 没搜到内容，{}s 后再试一次（共{}次）",
                    wait_sec, 2,
                )
                # #审查 #3 修复：原版 _time.sleep(5) 循环阻塞主线程 + 不响应
                # 取消信号，30+45s 累计 75s 长 sleep 在 video_pull 池（单 worker）
                # 里直接占住调度线程，其他任务调度延迟叠加。改用 interruptible_sleep
                # 支持取消信号立即唤醒，同时让 progress 模板反映倒计时。
                from app.services.task_service import interruptible_sleep
                import time as _time
                # 顶层 cancel-friendly sleep（切片前先做一次整体等待；cancel 时立即唤醒）
                interruptible_sleep(wait_sec, info)
                _wait_start = _time.time()
                while _time.time() - _wait_start < wait_sec:
                    _remaining = int(wait_sec - (_time.time() - _wait_start))
                    info.progress = _progress_phase_a_retry(
                        retry + 1, _remaining, int(_time.time() - round_start),
                    )
                    # interruptible_sleep 收到 cancel_requested 会抛 _TaskCancelled，
                    # 由外层 raise_for_cancel 一致处理；sleep 切片 1s 保持 progress 频率
                    interruptible_sleep(min(1, wait_sec - (_time.time() - _wait_start)), info)
                aweme_ids = search_session.search_all_for_ids(
                    keyword=keyword, conditions=conditions,
                    idle_timeout=60, max_pages=page_budget,
                    debug=load_settings().get("pull_debug", False),
                    progress_cb=_on_scroll_progress,
                )
                if aweme_ids:
                    retried_with_data = True
                    break
            if not retried_with_data:
                fail_reason = (
                    "搜索阶段未获取到任何视频（疑似风控或关键词无结果），"
                    "建议放宽过滤或重新启用任务"
                )
        # 阶段 A→B 切换标记（主路径 + retry 路径退出后统一刷一次）：
        # 避免 frontend 在阶段切换瞬间读到陈旧的"翻页采集中..."或"重试等待中..."残留，
        # 也避免 _run_detail_phase 第一帧"下载第 1/N 个 准备中"被误以为是阶段 A 的输出。
        if aweme_ids:
            info.progress = _progress_phase_a_done(
                len(aweme_ids), int(time.time() - round_start),
            )
        # ==================== 阶段 B：详情 + 下载 + 入库 ====================
        # 切换阶段标记：后续 _write_progress() 走「拉取中」模板
        _search_phase_active.clear()
        # #审查 #1：category_id / task_name 直接复用 _run_pull_round 已读取的
        # task 行引用，避免长任务阶段 A→B 期间被编辑（编辑落库后阶段 B 内
        # 二次 query 会读到新值，导致 total_pulled 累加按旧语义、实际入库落新分类）。
        if aweme_ids:
            # #161：阶段 B 复用阶段 A 的 page（同 persistent_context）抓详情。
            # 必须在 search_session.close() 之前取 page；close 后 self._page=None。
            # 同 page 复用避开了「独立 launch_persistent_context 同 profile_dir
            # chromium lock 冲突」+ 「独立 sync_playwright asyncio loop 冲突」。
            b_new, b_skip, b_limit, b_reason, detail_processed = _run_detail_phase(
                task_id, aweme_ids, conditions, client, cookie,
                account["id"], max_count, info,
                category_id=task["category_id"], task_name=task["task_name"],
                page=search_session.get_page(),
            )
            new_count += b_new
            skip_count += b_skip
            if b_limit:
                reached_limit = True
            # #124 阶段 B 内部异常跳出时记录 fail_reason（如登录失效/连续失败/风控）
            if b_reason:
                fail_reason = b_reason
            _write_progress()
    except LoginInvalidError:
        fail_reason = "登录态失效，跳过本轮"
        d.update_by_id("account", account["id"], {"status": "invalid"})
        notifier.notify("warn", "task", f"视频任务「{task['task_name']}」账号失效",
                        "本轮跳过", action=f"relogin:{account['id']}")
        _write_progress()
    except RiskControlError as e:
        fail_reason = f"风控拦截：{e}"
        _write_progress()
    except SearchBlockedError as e:
        # #审查修复：搜索风控带 page/keyword 上下文，便于排查
        kw = conditions.get("keyword", "") if isinstance(conditions, dict) else ""
        fail_reason = f"搜索接口风控（keyword={kw}）：{e}"
        logger.warning("[拉取任务] 任务「{}」搜索风控 keyword={} reason={}",
                       task["task_name"], kw, e)
        _write_progress()
    except Exception as e:  # noqa: BLE001
        fail_reason = str(e)
        _write_progress()
    finally:
        # #106 修复：先停 keepalive，避免 close session 阻塞期间 daemon 继续
        # tick 进已释放的闭包（search_session.close 耗时数秒，期间 keepalive
        # 会持续刷新 progress「用时」字段，用户看到秒数继续涨会误判卡死）
        _keepalive_stop.set()
        # #105 修复：finally 内三段清理平铺为独立 try/except。改造前嵌套在
        # 同一 try 内，close 异常被 except 吞后 Python 仍会跳出整个 finally，
        # 导致后面 bg_task_id 清零 + pull_round 累加被跳过——v22 阶梯统计会
        # 漏掉这一轮（pull_round 不 +1，total_pulled 不累加）。
        # 平铺后三段互不阻断，每段异常都记 debug 不影响 worker 退出。
        # 1) 关闭 search_session
        if search_session is not None:
            try:
                search_session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("[拉取任务] finally 关闭 search_session 异常：{}", e)
        # 2) 任务 #133：无论 round 成功/异常，bg_task_id 必清，
        #    避免删除任务时 request_cancel 命中已结束的 UUID。
        try:
            d.execute("UPDATE video_pull_task SET bg_task_id=NULL WHERE id=?", (task_id,))
        except Exception as e:  # noqa: BLE001 #91 finally 静默改为 debug
            logger.debug("[拉取任务] finally 清 bg_task_id 失败 task_id={} err={}", task_id, e)
        # 3) v22 阶梯式翻页：任何退出（达限 / has_more=0 / empty_pages /
        #    风控 / 异常）都 +1 round 并累加 total_pulled。用户决策：
        #    达 max_count 不清零；手动"重新启用"才重置。重置走 restart_pull_task，
        #    避免本页逻辑耦合太多状态分支。
        try:
            d.execute(
                "UPDATE video_pull_task "
                "SET pull_round = COALESCE(pull_round, 0) + 1, "
                "    total_pulled = COALESCE(total_pulled, 0) + ? "
                "WHERE id=? AND deleted=0",
                (new_count, task_id),
            )
        except Exception as e:  # noqa: BLE001 #91 finally 静默改为 debug
            logger.debug("[拉取任务] finally 累加 pull_round 失败 task_id={} err={}", task_id, e)
    # #107 注释：search_session 已在 finally 中关闭，下面是 scheduler / DB 操作，
    # 不要在此处误用 search_session 对象（已释放会触发 None 检查跳过）。
    # 例外：_captured / _total_captured_pages 是普通 list/int 字段，
    # close() 不清空，可安全读取（终态 progress 用 _total_captured_pages）。
    # 达到 max_count 上限：自动停用任务（移除调度 + 改状态）
    if reached_limit and not fail_reason:
        task_scheduler.remove_job(_JOB_PREFIX + task_id)
        d.update_by_id("video_pull_task", task_id, {
            "status": "disabled",
            "next_run_time": None,
            "stop_reason": "auto_max",  # v23：前端列表据此显示"重新启用"按钮
        })
        fail_reason = f"已达单轮上限 {max_count} 条，任务自动停用"

    _write_pull_log(task_id, new_count, skip_count, fail_reason)
    # PR3 #56：实时即终态，最后一次循环内的 info.progress 即为终态（删除"总页数 ..."覆写）
    # #遗漏 #1：507 改造后已无逐页 page 计数（原 page 变量永远 =1），日志改用
    # 阶段 B 实际处理视频数。
    logger.info(
        "[拉取] 任务「{}」已结束：处理 {} 个，新增 {}，跳过 {}{}",
        task["task_name"], detail_processed, new_count, skip_count,
        f" / 原因：{fail_reason}" if fail_reason else "",
    )
    # 终态 progress 模板（覆盖 #56 实时即终态的旧设计 — 阶段 B 每条 progress
    # 都被新值刷新，最后一眼是 "下载第 N/N 个 下载入库中"，用户看不到总览；
    # 这里统一刷一次终态：总页数 / 新增 / 拦截 / 用时）。
    # 总页数取 search_session._total_captured_pages（跨 search 累计字段）：
    # _captured 在每次 search_all_for_ids 入口 .clear()，retry 路径下只反映
    # 最后一次结果；_total_captured_pages 在 search 出口累加 len(_captured)，
    # 所以 retry 后总页数 = retry 前 + retry 后，不丢失。
    _total_pages = (
        search_session._total_captured_pages if search_session else 0
    )
    info.progress = _progress_terminal(
        _total_pages, new_count, skip_count, int(time.time() - round_start),
    )
    # 任务 #367：信息列直接显示抓取详情(fail_reason),task_service 保留 info.message
    info.message = fail_reason or ""
    return "partial" if fail_reason else "success"


def _extract_local_cover(video_path: Path, material_id: str,
                         material_type: str = "video",
                         duration_ms: int | None = None) -> str:
    """从本地视频抽取首帧作为封面（分辨率=视频原生分辨率；#364 落 material/<type>/<date>/<id>/）。

    自适应 seek（fix：iPhone 录的极短预览 < 1s 时 seek=1.0 会落到视频外 → mjpeg encoder EOF
    失败 → rc=-22，封面永远抽不到）：
    - duration_ms < 1000 → seek=0（首帧）
    - 1000 ≤ duration_ms < 3000 → seek = duration_ms / 2（中段）
    - duration_ms ≥ 3000 → seek = 1.0
    - duration_ms 缺失（兼容老调用）→ seek = 1.0（保持原行为）

    返回相对 data 根的路径（material/<type>/<date>/<id>/<id>_cover.jpg）；
    失败返回空串，调用方按各自兜底策略（upload 撤回入库，pull/share 走 CDN 兜底）。
    """
    from app.core.ffmpeg import extract_frame
    rel = _build_material_cover_path(material_type, material_id, ".jpg")
    abs_path = get_data_dir() / rel
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("[封面] 缓存目录创建失败 material_id={} err={}", material_id, e)
        return ""

    if duration_ms is None or duration_ms <= 0:
        seek_s = 1.0
    elif duration_ms < 1000:
        seek_s = 0.0  # 短视频取首帧（避免 seek 落到视频外）
    elif duration_ms < 3000:
        seek_s = duration_ms / 2000.0  # 1-3s 取中点（首帧可能黑场）
    else:
        seek_s = 1.0

    if extract_frame(str(video_path), str(abs_path), seek_seconds=seek_s):
        return rel
    logger.warning("[封面] 本地抽帧失败 material_id={} video={} duration_ms={} seek={:.2f}s",
                   material_id, video_path.name, duration_ms, seek_s)
    return ""


def _match_conditions(video: dict, conditions: dict) -> bool:
    """客户端条件兜底：标题正则 / 画面方向 / 发布时间档位 / 视频时长档位。

    抖音搜索接口只支持 keyword + offset，其余条件必须在客户端按搜索返回字段二次过滤。
    不支持的旧字段（topics/desc_keywords/location/shop_name）仅作兼容读取，不做硬过滤（抖音 API 未返回结构化字段）。
    """
    pattern = conditions.get("title_regex", "").strip()
    if pattern:
        try:
            # v126 扩展：title / desc / topics 三字段或匹配，任一命中即放行
            # title 是 desc 第一行截 50 字，可能与 desc 重叠；topics 是 "#话题1#话题2" 拼接
            text_pool = "\n".join(filter(None, [
                video.get("title", "") or "",
                video.get("desc", "") or "",
                video.get("topics", "") or "",
            ]))
            if text_pool and not re.search(pattern, text_pool):
                return False
        except re.error as e:  # noqa: PERF203 #96 silent → warning + set 防刷屏
            key = (pattern, str(e))
            if key not in _RE_ERR_LOGGED:
                _RE_ERR_LOGGED.add(key)
                logger.warning(
                    "[过滤条件] title_regex 编译失败 pattern={!r} err={}（该条视频按未匹配处理）",
                    pattern, e,
                )
    orientation = conditions.get("orientation", "")
    if orientation:
        # 任务 #133 复盘修复误拉横屏：
        # 优先用 orientation 字段；缺失时（_parse_search_item 三源全无 width/height
        # 才返回 None）按 width/height 二次判定。双重校验避免 dimension 缺失的
        # 横屏视频被 _parse_search_item 误标 vertical 入库。
        vo = video.get("orientation")
        if vo is None:
            w, h = int(video.get("width") or 0), int(video.get("height") or 0)
            if w and h:
                vo = "horizontal" if w >= h else "vertical"
        if vo and vo != orientation:
            return False
    # 发布时间档位（相对当前时间）：publish_time 为毫秒时间戳或 ISO 字符串
    pr = conditions.get("publish_range") or "any"
    days = PUBLISH_RANGE_OPTIONS.get(pr)
    if days is not None:
        ts = _parse_publish_ts(video.get("publish_time"))
        if ts is not None and not _within_days(ts, days):
            return False
    # 时长档位（max/min 阈值；max_eq/min_eq 决定边界是否包含）
    dr = conditions.get("duration_range") or "any"
    bounds = DURATION_RANGE_OPTIONS.get(dr) or DURATION_RANGE_OPTIONS["any"]
    max_ms = bounds["max"]
    min_ms = bounds["min"]
    if max_ms is not None or min_ms is not None:
        dms = _parse_duration_ms(video.get("duration_ms"))
        if dms is None:
            # 时长未知 + 档位过滤严格 → 放行（与「检测失败默认放行」一致）
            pass
        else:
            if max_ms is not None:
                if bounds["max_eq"]:
                    if dms > max_ms:
                        return False
                else:
                    if dms >= max_ms:
                        return False
            if min_ms is not None:
                if bounds["min_eq"]:
                    if dms < min_ms:
                        return False
                else:
                    if dms <= min_ms:
                        return False
    return True


def _which_blocked(video: dict, conditions: dict) -> str:
    """诊断视频被哪个客户端兜底条件拦截（v126 拉取日志用）。

    与 `_match_conditions` 逻辑保持一致：按 title_regex / orientation /
    publish_range / duration_range 顺序检查，返回首个不满足条件的可读字符串。
    未拦截返回空串。

    返回示例：
        "title_regex(= '探店|测评')"
        "orientation(需要 vertical, 实际 horizontal)"
        "publish_range(需要 1d, 实际 2024-08-01 12:34)"
        "duration_range(需要 lt1m, 时长 90.0s)"
    """
    pattern = conditions.get("title_regex", "").strip()
    if pattern:
        try:
            text_pool = "\n".join(filter(None, [
                video.get("title", "") or "",
                video.get("desc", "") or "",
                video.get("topics", "") or "",
            ]))
            if text_pool and not re.search(pattern, text_pool):
                return f"title_regex(= {pattern!r})"
        except re.error as e:  # noqa: PERF203 #96 silent → warning + set 防刷屏（同一 pattern 首处已记）
            key = (pattern, str(e))
            if key not in _RE_ERR_LOGGED:
                _RE_ERR_LOGGED.add(key)
                logger.warning(
                    "[过滤条件] title_regex 编译失败 pattern={!r} err={}",
                    pattern, e,
                )
    orientation = conditions.get("orientation", "")
    if orientation:
        vo = video.get("orientation")
        if vo is None:
            w, h = int(video.get("width") or 0), int(video.get("height") or 0)
            if w and h:
                vo = "horizontal" if w >= h else "vertical"
        if vo and vo != orientation:
            return f"orientation(需要 {orientation}, 实际 {vo})"
    pr = conditions.get("publish_range") or "any"
    days = PUBLISH_RANGE_OPTIONS.get(pr)
    if days is not None:
        ts = _parse_publish_ts(video.get("publish_time"))
        if ts is not None and not _within_days(ts, days):
            from datetime import datetime
            try:
                actual = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                actual = str(ts)
            return f"publish_range(需要 {pr}={days}天内, 实际 {actual})"
    dr = conditions.get("duration_range") or "any"
    bounds = DURATION_RANGE_OPTIONS.get(dr) or DURATION_RANGE_OPTIONS["any"]
    if bounds["max"] is not None or bounds["min"] is not None:
        dms = _parse_duration_ms(video.get("duration_ms"))
        if dms is not None:
            if bounds["max"] is not None:
                over = (bounds["max_eq"] and dms > bounds["max"]) or (
                    not bounds["max_eq"] and dms >= bounds["max"]
                )
                if over:
                    return f"duration_range(需要 {dr}, 时长 {dms / 1000:.1f}s)"
            if bounds["min"] is not None:
                under = (bounds["min_eq"] and dms < bounds["min"]) or (
                    not bounds["min_eq"] and dms <= bounds["min"]
                )
                if under:
                    return f"duration_range(需要 {dr}, 时长 {dms / 1000:.1f}s)"
    return ""


def _parse_publish_ts(value) -> int | None:
    """解析 publish_time 字段为毫秒时间戳；接受秒整数 / 毫秒整数 / ISO 字符串，失败返回 None。

    抖音 create_time 接口统一为秒（实测），但保留毫秒兼容（> 10^12 视为毫秒）。
    字符串纯数字走同样的秒/毫秒判定。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 10**12 else n * 1000
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            from datetime import datetime
            # ISO 字符串（抖音接口常见格式：2024-01-01 12:00:00 或 2024-01-01T12:00:00）
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try:
                    return int(datetime.strptime(s[:19], fmt).timestamp() * 1000)
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            return None
        # 字符串纯数字：抖音默认秒，> 10^12 视为毫秒
        try:
            n = int(float(s))
            return n if n > 10**12 else n * 1000
        except (TypeError, ValueError):
            return None
    return None


def _within_days(ts_ms: int, days: int) -> bool:
    """发布时间是否在距今 N 天内（含 N 天边界）。"""
    import time as _time
    now_ms = int(_time.time() * 1000)
    return ts_ms >= now_ms - days * 24 * 3600 * 1000


def _parse_duration_ms(value) -> int | None:
    """解析 duration_ms 字段（毫秒整数）；失败返回 None。"""
    if value is None:
        return None
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _download_bgm(video: dict, client, source_type: str = "pull") -> None:
    """视频入库后同步拉取其背景音轨入音乐库（未分类）。

    仅下载抖音 music 节点的 play_url 直链（纯 BGM 源文件，不含作者人声）；
    版权受限曲目无直链 → 直接跳过。同一首原声多视频共用 music.mid，按其去重。
    #592 统一转 mp3：原格式非 mp3 → ffmpeg 转码（libmp3lame q=2）。
    任何失败只记日志，不影响视频入库结果。
    """
    m = video.get("music") or {}
    url = m.get("download_url") or ""
    if not url:
        return
    from app.db.utils import new_id
    d = get_db()
    music_id = m.get("music_id") or ""
    # 按原声 ID 去重：同一首原声已入过库则跳过
    if music_id and d.query_one(
            "SELECT id FROM material WHERE source_ref=? AND deleted=0", (music_id,)):
        return
    material_id = new_id()
    # 扩展名从直链提取（#592 改：内联 tuple 改用顶层 MUSIC_EXTS，避免漏改）
    ext = ".mp3"
    low = url.split("?")[0].lower()
    for e in MUSIC_EXTS:
        if low.endswith(e):
            ext = e
            break
    save_path = _build_material_path("music", "", material_id, "", ext)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_video(url, str(save_path))
    except BaseException:
        # #P1-1-#592：下载失败统一走 helper 清理
        _cleanup_material_files(_safe_data_rel(save_path))
        raise
    # #592 统一转 mp3：原格式非 mp3 → ffmpeg 转码
    if ext != ".mp3":
        transcoded = _transcode_audio_to_mp3(save_path)
        if transcoded is None:
            _cleanup_material_files(_safe_data_rel(save_path))
            logger.warning("[BGM] 转 mp3 失败 music_id={} → 跳过入库", music_id)
            return
        # 用转码产物替换源文件，统一存 mp3
        save_path.unlink(missing_ok=True)
        transcoded.rename(save_path.with_suffix(".mp3"))
        save_path = save_path.with_suffix(".mp3")
        ext = ".mp3"
    md5 = _md5_of_file(save_path)
    # MD5 兜底去重（music_id 缺失时同一文件可能重复入库）
    if d.query_one("SELECT id FROM material WHERE file_md5=? AND deleted=0", (md5,)):
        _cleanup_material_files(_safe_data_rel(save_path))
        return
    # ffprobe 探测时长（失败回退接口给的 duration）
    probe = extract_media_info(probe_media(str(save_path))) \
        if save_path.stat().st_size > 1024 else {}
    # 封面：#364 下载到 material/music/<date>/<id>/<id>_cover.<ext>
    cover_url = m.get("cover_url") or ""
    cover_ext = _infer_image_ext(cover_url, default=".webp")
    cover_rel = ""
    if cover_url:
        from app.core.douyin.avatar_cache import download_to
        cover_rel = download_to(cover_url, _build_material_cover_path("music", material_id, cover_ext))
    file_rel = str(save_path.relative_to(get_data_dir())).replace("\\", "/")
    d.insert("material", {
        "id": material_id,
        "title": m.get("title") or "未知原声",
        "author_title": m.get("title"),
        "type": "music",
        "category_id": UNCATEGORIZED_ID,  # 入「未分类」虚拟分类
        "file_size": save_path.stat().st_size,
        "source_type": source_type,
        "source_ref": music_id or None,
        "file_md5": md5,
        "file_path": file_rel,
        "duration_ms": probe.get("duration_ms") or m.get("duration_ms") or None,
        "author_nickname": m.get("author") or None,
        # 音频直链落库（详情溯源展示；带签名会过期）
        "download_url": url,
        # 原声聚合页链接（#45：mid 拼接）
        "share_url": m.get("share_url") or None,
        # 音乐封面本地缓存相对路径（#43/#44）
        "cover_url": cover_rel or None,
    })
    logger.info("[BGM] 已入库音轨「{}」（来源视频 {}）", m.get("title"), video.get("video_id"))


# ---------- 单音乐统一处理（任务 #132：分享音乐链接入库）----------

def _process_single_music(
    music: dict,
    category_id: str,
    client,
    *,
    source: str = "share",
) -> dict:
    """单音乐统一处理：去重 → 下载音频 → 入音乐库（任务 #132）。

    分享链接 `/music/{mid}` 解析后走此函数。music 字段来自 `_parse_music_info`：
    - music_id / title / author / author_id / author_handle
    - duration_ms / download_url / share_url / cover_url

    返回:
        {"action": "new"/"duplicate"/"failed", "material_id", "title", "reason"}
    """
    music_id = music.get("music_id") or ""
    title = music.get("title") or "未知原声"

    # 1) 去重（按 music_id；同一首原声已入过库则跳过）
    if music_id:
        d = get_db()
        existing = d.query_one(
            "SELECT id, title FROM material WHERE source_ref=? AND deleted=0", (music_id,))
        if existing:
            return {
                "action": "duplicate",
                "material_id": existing["id"],
                "title": existing.get("title") or title,
                "reason": f"已存在（素材：{existing['title']}）",
            }

    # 2) 下载音频 + 入库
    try:
        material_id = _download_music_ingest(music, category_id, client, source_type=source)
    except DouyinClientError as e:
        return {
            "action": "failed",
            "material_id": None,
            "title": title,
            "reason": f"抖音接口异常：{e}",
        }
    if not material_id:
        return {
            "action": "failed",
            "material_id": None,
            "title": title,
            "reason": "音乐下载失败或入库异常",
        }
    return {
        "action": "new",
        "material_id": material_id,
        "title": title,
        "reason": None,
    }


def _download_music_ingest(music: dict, category_id: str, client,
                          *, source_type: str = "share") -> str:
    """下载音频并入音乐库（任务 #132）。

    与 `_download_bgm` 的差异：
    - 用途：分享链接直接入音乐库（非视频附属 BGM）
    - 入库字段：author_nickname 用音乐作者（music.author）而非视频作者
    - share_url 必填（原声聚合页链接）
    - #592 统一转 mp3（原格式非 mp3 → ffmpeg 转码）
    """
    from app.db.utils import new_id

    music_id = music.get("music_id") or ""
    title = music.get("title") or "未知原声"
    download_url = music.get("download_url") or ""
    if not download_url:
        raise DouyinClientError("音乐直链为空，版权受限曲目无法下载")

    # 扩展名从直链提取（#592：内联 tuple 改顶层 MUSIC_EXTS）
    ext = ".mp3"
    low = download_url.split("?")[0].lower()
    for e in MUSIC_EXTS:
        if low.endswith(e):
            ext = e
            break

    material_id = new_id()
    save_path = _build_material_path("music", category_id, material_id, title, ext)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    # 复用 client.download_video（纯 HTTP UA+Referer，与 BGM 一致）
    try:
        client.download_video(download_url, str(save_path))
    except BaseException:
        # #P1-1-#592：下载失败统一走 helper 清理
        _cleanup_material_files(_safe_data_rel(save_path))
        raise

    # #592 统一转 mp3：原格式非 mp3 → ffmpeg 转码
    if ext != ".mp3":
        transcoded = _transcode_audio_to_mp3(save_path)
        if transcoded is None:
            _cleanup_material_files(_safe_data_rel(save_path))
            return ""
        save_path.unlink(missing_ok=True)
        transcoded.rename(save_path.with_suffix(".mp3"))
        save_path = save_path.with_suffix(".mp3")
        ext = ".mp3"

    md5 = _md5_of_file(save_path)
    # MD5 兜底去重
    d = get_db()
    if d.query_one("SELECT id FROM material WHERE file_md5=? AND deleted=0", (md5,)):
        _cleanup_material_files(_safe_data_rel(save_path))
        return ""

    # ffprobe 探测时长（失败回退接口给的 duration）
    probe = extract_media_info(probe_media(str(save_path))) \
        if save_path.stat().st_size > 1024 else {}

    # 封面本地缓存（#364：下载到 material/music/<date>/<id>/<id>_cover.<ext>）
    cover_url = music.get("cover_url") or ""
    cover_rel = ""
    if cover_url:
        cover_ext = _infer_image_ext(cover_url, default=".webp")
        from app.core.douyin.avatar_cache import download_to
        cover_rel = download_to(cover_url, _build_material_cover_path("music", material_id, cover_ext))

    file_rel = str(save_path.relative_to(get_data_dir())).replace("\\", "/")
    d.insert("material", {
        "id": material_id,
        "title": title,
        "author_title": title,
        "type": "music",
        "category_id": category_id,
        "file_size": save_path.stat().st_size,
        "source_type": source_type,
        "source_ref": music_id or None,
        "file_md5": md5,
        "file_path": file_rel,
        "duration_ms": probe.get("duration_ms") or music.get("duration_ms") or None,
        # 任务 #132：音乐作者（非视频作者）
        "author_nickname": music.get("author") or None,
        "author_douyin_id": music.get("author_handle") or None,
        # 音乐直链落库
        "download_url": download_url,
        # 原声聚合页链接
        "share_url": music.get("share_url") or None,
        # 音乐封面本地缓存
        "cover_url": cover_rel or None,
    })
    logger.info("[音乐导入] 已入库音轨「{}」（music_id={}）", title, music_id)
    return material_id


def _download_and_ingest(video: dict, category_id: str, client,
                         conditions: dict | None = None,
                         source_type: str = "pull",
                         info: Optional[Any] = None) -> str:
    """下载视频并入库（拉取/分享共用）。

    参数:
        video: 统一视频字段 dict（client.search_videos / resolve_share 返回）
        category_id: 入库分类
        client: 抖音客户端
        conditions: 拉取任务条件 dict（仅拉取任务传入，用于字幕/主播人脸过滤与 BGM 开关）；
            分享导入场景传 None，跳过内容检测与 BGM。
        source_type: 来源标记 pull（拉取任务）/ share（分享导入）
        info: 任务信息（worker 上下文）；提供时重试 sleep 改为 interruptible_sleep
            响应取消信号，传 None 时降级 time.sleep（分享导入场景无 worker info）。
    返回:
        material_id；过滤命中时返回空串 ""（调用方据此计入 skip_count）。
    """
    from app.db.utils import new_id
    from app.core.douyin.avatar_cache import cache_avatar, cache_cover
    d = get_db()
    material_id = new_id()
    save_path = _build_material_path("video", category_id, material_id, video["title"], ".mp4")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    # 任务 #508：本地路径 / CDN 节点 URL 两条日志移到 _download_with_retry 内部
    # （入口打本地路径 + 每节点尝试前打 URL，节点切换更清晰）
    # curl_cffi 下载大文件偶现 curl: (35) Connection reset
    # （抖音 CDN 节点切换/瞬时风控），原版无重试直接失败。
    # 本次优化：detail 接口 download_addr.url_list 含 3 个 CDN 节点备份，
    # 节点偶发连接重置/超时 → 遍历 download_urls 逐节点尝试，每个节点 1 次重试。
    # 不可重试：「疑似风控页」（连续重试无意义）/ HTTP 4xx（资源不存在/拒访）。
    def _download_with_retry() -> None:
        # CDN fallback 列表：download_urls（detail 接口 video 节点全部直链，按无水印优先拼接）→
        # download_url（主节点，单 URL 兜底，兼容旧版调用方直接传单 URL 的情况）。
        # 元素形态：(field, url) 元组，field 是 video 节点字段名便于排查节点归属；
        # 兼容旧形态（裸 URL 字符串）—— auto-detect 后 normalize。
        raw = video.get("download_urls") or []
        candidates: list[tuple[str, str]] = []
        for item in raw:
            if isinstance(item, tuple) and len(item) == 2:
                candidates.append((item[0], item[1]))
            elif isinstance(item, str) and item:
                candidates.append(("legacy", item))
        if not candidates and video.get("download_url"):
            candidates.append(("download_url", video["download_url"]))
        if not candidates:
            save_path.unlink(missing_ok=True)
            _cleanup_empty_dir(save_path.parent)
            raise DouyinClientError("视频直链为空，无法下载")
        # C1: 入口单条 INFO 汇总候选 URL + 本地路径，调试时一次看清所有备选节点，
        # 避免分散 3 条日志（本地路径 + 每节点尝试前的 URL）。
        # URL 截断到首尾特征（默认 head=60 + tail=20），中间省略号——长 URL
        # 含签名 query string 拼接到单条 INFO 1.5KB+ 影响控制台可读性。
        def _shorten_url(u: str, head: int = 60, tail: int = 20) -> str:
            if len(u) <= head + tail + 3:
                return u
            return f"{u[:head]}...{u[-tail:]}"

        url_list = " | ".join(
            f"[{i + 1}/{len(candidates)}]{f}={_shorten_url(u)}"
            for i, (f, u) in enumerate(candidates)
        )
        logger.info(
            "[下载] 视频 {} 候选 {} 个 → {} | 本地={}",
            video.get("video_id"), len(candidates), url_list, save_path,
        )
        # 每个 URL 内最多 1 次重试（避免在挂掉的节点上空耗）
        MAX_URL_RETRIES = 1
        last_err: Exception | None = None
        for idx, (field, url) in enumerate(candidates):
            for attempt in range(MAX_URL_RETRIES + 1):
                try:
                    client.download_video(url, str(save_path))
                    if idx > 0:
                        logger.info(
                            "[下载] 视频 {} CDN fallback 成功 field={} [{}/{}]",
                            video.get("video_id"), field, idx + 1, len(candidates),
                        )
                    return
                except DouyinClientError as e:
                    last_err = e
                    msg = str(e)
                    # 不可重试错误：风控页 / HTTP 4xx（用正则匹配状态码独立数字，
                    # 避免「Timeout 4000ms」之类误匹配「400」）→ 换下一 CDN 节点
                    # 残文件由 client.download_video 内部 unlink 清理（DRY 单一职责）
                    if "疑似风控页" in msg or re.search(r"\b(400|401|403|404)\b", msg):
                        logger.warning(
                            "[下载] 视频 {} field={} [{}/{}] 不可重试（{}）→ 换下一节点",
                            video.get("video_id"), field, idx + 1, len(candidates), msg[:80],
                        )
                        break  # 跳出当前 URL 内重试循环，轮到下一 URL
                    # 已用尽当前 URL 的重试 → 换下一 CDN 节点
                    if attempt >= MAX_URL_RETRIES:
                        logger.warning(
                            "[下载] 视频 {} field={} [{}/{}] 重试 {} 次仍失败 → 换下一节点",
                            video.get("video_id"), field, idx + 1, len(candidates), MAX_URL_RETRIES,
                        )
                        break
                    # C1: 退避重试中间步骤不再打 warning（reason 在下次循环
                    # 「重试仍失败」warning 必现，避免重复日志）。
                    wait_sec = 1 + attempt * 2  # 1s, 3s
                    if info is not None:
                        interruptible_sleep(wait_sec, info)
                    else:
                        time.sleep(wait_sec)
        # 所有 CDN 节点都失败（残文件清理交给外层 except BaseException 统一处理，DRY）
        raise DouyinClientError(
            f"视频下载失败：所有 {len(candidates)} 个 CDN 节点均不可用"
        ) from last_err

    try:
        _download_with_retry()
    except BaseException:
        # #P1-1-#592：下载失败统一走 helper 清理
        _cleanup_material_files(_safe_data_rel(save_path))
        raise

    # #592：统一转 mp4 + 补全元数据（无音频轨时加 anullsrc 静音轨）
    # 转码失败的统一路径：撤销入库（清理文件 + 返回 ""）
    transcoded = _transcode_video_to_mp4(save_path)
    if transcoded is None:
        _cleanup_material_files(_safe_data_rel(save_path))
        logger.warning("[拉取] 视频转 mp4 失败 video_id={} → 撤销入库",
                       video.get("video_id"))
        return ""
    # 用转码产物替换原下载文件，统一存 mp4
    save_path.unlink(missing_ok=True)
    transcoded.rename(save_path)

    # 内容检测（字幕/主播人脸）：下载后、入库前按需过滤；命中则清理下载文件后返回空串
    need_subtitle = bool(conditions and conditions.get("filter_subtitle"))
    need_face = bool(conditions and conditions.get("filter_face"))
    if need_subtitle or need_face:
        from app.core.media_inspect import inspect_video
        try:
            has_subtitle, has_face, sub_texts, inspect_reason = inspect_video(
                str(save_path), material_id,
                need_subtitle=need_subtitle, need_face=need_face,
            )
            video_id = video.get("video_id") or "未知"  # 防御 video dict 缺字段（None / 空串都走兜底）
            # v126 三态语义：
            # - True  = 命中 → 拒绝入库
            # - False = 未命中 → 放行
            # - None  = 抽帧失败无法判定 → 严格拒绝（避免漏过滤，
            #          覆盖 VideoCorruptedError / VideoNoStreamError）
            if need_subtitle and has_subtitle is not False:
                reason = "命中字幕" if has_subtitle else _INSPECT_REASON_TEXT.get(
                    inspect_reason, "内容检测抽帧失败"
                )
                return _reject_and_cleanup(save_path, video_id, reason, sub_texts)
            if need_face and has_face is not False:
                reason = "命中主播人脸" if has_face else _INSPECT_REASON_TEXT.get(
                    inspect_reason, "内容检测抽帧失败"
                )
                return _reject_and_cleanup(save_path, video_id, reason)
        except Exception as e:  # noqa: BLE001
            # 检测异常默认放行，不阻断主流程
            logger.warning("[内容检测] 异常，默认放行：{}", e)

    md5 = _md5_of_file(save_path)
    # ffprobe 探测（失败字段置空，F-03-R2）
    probe = extract_media_info(probe_media(str(save_path))) if save_path.stat().st_size > 1024 else {}
    # #592 封面：仅本地抽帧，失败 → 撤销入库（删文件 + 不调 CDN 兜底）
    cover_rel = _extract_local_cover(save_path, material_id, "video",
                                     duration_ms=probe.get("duration_ms") if probe else None)
    if not cover_rel:
        _cleanup_material_files(_safe_data_rel(save_path))
        logger.warning("[拉取] 封面抽帧失败 video_id={} → 撤销入库",
                       video.get("video_id"))
        return ""
    # #364：作者头像下载到 material/video/<date>/<id>/<id>_avatar.<ext>
    avatar_url = video.get("author_avatar", "")
    avatar_rel = ""
    if avatar_url:
        avatar_ext = _infer_image_ext(avatar_url, default=".webp")
        from app.core.douyin.avatar_cache import download_to
        avatar_rel = download_to(avatar_url, _build_material_avatar_path("video", material_id, avatar_ext))
    d.insert("material", {
        "id": material_id,
        "title": video["title"],
        "type": "video",
        "category_id": category_id,
        "file_size": save_path.stat().st_size,
        "source_type": source_type,
        "source_ref": video["video_id"],
        "file_md5": md5,
        "file_path": str(save_path.relative_to(get_data_dir())).replace("\\", "/"),
        "duration_ms": probe.get("duration_ms") or video.get("duration_ms"),
        "orientation": probe.get("orientation") or video.get("orientation"),
        "resolution": probe.get("width") and probe.get("height")
                      and f"{probe['width']}x{probe['height']}" or video.get("resolution"),
        # 出处信息（详情展示：作者/原视频/下载直链/封面）
        "author_nickname": video.get("author_nickname") or None,
        "author_douyin_id": video.get("author_douyin_id") or None,
        "author_title": video.get("author_title") or video.get("title") or None,
        "share_url": video.get("share_url") or None,
        "download_url": video.get("download_url") or None,
        "cover_url": cover_rel or None,
        # 出处补充（v6：头像 + 互动快照 + 发布时间）
        "author_avatar": avatar_rel or None,
        # 作者增强（v10：主页 sec_uid + 简介 + 粉丝/获赞）
        "author_sec_uid": video.get("author_sec_uid") or None,
        "author_signature": video.get("author_signature") or None,
        "author_follower_count": video.get("author_follower_count") or 0,
        "author_total_favorited": video.get("author_total_favorited") or 0,
        "digg_count": video.get("digg_count") or 0,
        "comment_count": video.get("comment_count") or 0,
        "collect_count": video.get("collect_count") or 0,
        "share_count": video.get("share_count") or 0,
        "publish_time": video.get("publish_time") or None,
    })
    # 背景音轨同步入库：仅拉取任务且 fetch_bgm=True 时执行
    fetch_bgm = bool(conditions.get("fetch_bgm")) if conditions else False
    if fetch_bgm:
        try:
            _download_bgm(video, client, source_type)
        except Exception as e:  # noqa: BLE001 BGM 属附加产物，任何失败静默降级
            logger.warning("[BGM] 音轨拉取失败（视频 {}）：{}", video.get("video_id"), str(e)[:200])
    return material_id


def _write_pull_log(task_id: str, new_count: int, skip_count: int, fail_reason: str) -> None:
    """视频拉取执行日志。"""
    get_db().insert("video_pull_log", {
        "task_id": task_id, "run_time": now_str(), "new_count": new_count,
        "skip_count": skip_count, "fail_reason": fail_reason or None})


# ---------- 单视频统一处理（任务 #130 / #131）----------

def _process_single_video(
    video: dict,
    category_id: str,
    client,
    *,
    source: str,
    conditions: dict | None = None,
    cookie: str = "",
    account_id: str = "",   # #P0-8：拉取侧补抓详情走 storage_state 路径
    info: Optional[Any] = None,
) -> dict:
    """单视频统一处理：拉取侧补抓详情 → 客户端兜底过滤 → 去重 → 下载入库。

    任务 #131：拉取/分享统一走浏览器抓 detail。
    - 拉取（source='pull'）：video 来自搜索接口（_parse_search_item 输出，author 字段不全
      无粉丝/获赞），客户端按 desc/title/topics 做初筛（title_regex/orientation/
      publish_range/duration_range），通过后调 client._fetch_aweme_detail 重抓补全字段
      （含 author.follower_count/total_favorited）。
    - 分享（source='share'）：video 来自 _fetch_aweme_detail（已是完整 54 字段 author），
      conditions=None 跳过所有过滤，直接下载入库。

    参数:
        video: 统一视频字段 dict
        category_id: 入库分类 ID
        client: 抖音客户端（提供 _fetch_aweme_detail 公共方法）
        source: 'pull' / 'share'
        conditions: 拉取条件 dict（仅拉取侧传入）
        cookie: 拉取账号 cookie（拉取侧补抓详情用；分享侧用 _share_cookie 自取）
    返回:
        {"action": "new"/"filtered"/"duplicate"/"failed", "material_id", "title", "reason"}
    """
    # 1) 客户端兜底过滤（仅拉取侧；分享侧 conditions=None 跳过）
    if conditions is not None and not _match_conditions(video, conditions):
        return {
            "action": "filtered",
            "material_id": None,
            "title": video.get("title", ""),
            "reason": _which_blocked(video, conditions) or "客户端兜底",
        }
    # 2) 去重（用 video_id 直接判；detail 抓取前先短路，避免无谓开浏览器）
    dup = _duplicate_check(video["video_id"], "")
    if dup:
        return {
            "action": "duplicate",
            "material_id": dup["id"],
            "title": dup.get("title", ""),
            "reason": f"已存在（素材：{dup['title']}）",
        }
    # 3) 任务 #131：拉取侧补抓详情（拿完整 author 字段 + 准确 download_url）。
    #    分享侧 video 已是 detail 输出（含 _source='detail'），跳过。
    # #P0-8：传 account_id 让 _fetch_aweme_detail 走 storage_state 路径
    if source == "pull" and video.get("_source") != "detail":
        try:
            video = client._fetch_aweme_detail(video["video_id"], cookie, account_id=account_id)
        except DouyinClientError as e:
            return {
                "action": "failed",
                "material_id": None,
                "title": video.get("title", ""),
                "reason": f"详情抓取失败：{e}",
            }
    # 4) 下载 + 内容过滤 + 入库
    try:
        material_id = _download_and_ingest(
            video, category_id, client, conditions, source_type=source, info=info)
    except DouyinClientError as e:
        return {
            "action": "failed",
            "material_id": None,
            "title": video.get("title", ""),
            "reason": f"抖音接口异常：{e}",
        }
    # _download_and_ingest 内部字幕/人脸命中或抽帧失败时返空串（计入过滤）
    if not material_id:
        return {
            "action": "filtered",
            "material_id": None,
            "title": video.get("title", ""),
            "reason": "内容过滤命中（字幕/主播人脸）或抽帧失败",
        }
    return {
        "action": "new",
        "material_id": material_id,
        "title": video.get("title", ""),
        "reason": None,
    }


def run_pull_task_now(task_id: str) -> str:
    """手动触发一轮视频拉取。"""
    # PR2 #55：type 改为 video_pull；name 取 video_pull_task.task_name（任务被删时回退"未命名任务"）。
    row = get_db().query_one("SELECT task_name FROM video_pull_task WHERE id=?", (task_id,))
    name = row["task_name"] if row else "未命名任务"
    bg_task_id = task_service.submit(
        "video_pull", name,
        lambda info: _run_pull_round(task_id, info))
    # 任务 #133：手动触发也写 bg_task_id，删除时可取消
    try:
        get_db().execute(
            "UPDATE video_pull_task SET bg_task_id=? WHERE id=?",
            (bg_task_id, task_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("[拉取任务] 手动触发写 bg_task_id 失败 {}：{}", task_id, e)
    return bg_task_id


# ---------- 507 改造：阶段 B —— 详情+下载+入库 ----------

def _run_detail_phase(
    task_id: str,
    aweme_ids: list[str],
    conditions: dict,
    client,
    cookie: str,
    account_id: str,
    max_count: int,
    info,
    *,
    category_id: str,
    task_name: str,
    page=None,
) -> tuple[int, int, int, bool, str, int]:
    """507 改造：阶段 B —— 顺序循环 aweme_ids，详情抓取 + 内容校验 + 下载入库。

    page：阶段 A BrowserSearchSession 的 page（同 persistent_context）；
    阶段 B 复用避免 chromium 同 profile_dir lock 冲突 + asyncio loop 冲突。
    不传时回退到 BrowserActor 独立 sync_playwright 路径（分享导入等场景）。

    返回: (new_count, skip_count, reached_limit, fail_reason, processed_count)
    每个视频独立异常不影响后续视频（每条 try/except）。

    processed_count 包含所有实际尝试处理的视频（含 fail），供外层日志/统计用。

    category_id / task_name 由 _run_pull_round 注入（#审查 #1：避免长任务
    A→B 间隙被编辑后阶段 B 内 query 读到新值导致语义错位）。

    507 #124 异常分类：
    - LoginInvalidError：账号登录态失效 → fail_reason 提示，账号置 invalid
    - RiskControlError：风控拦截 → fail_reason 提示，跳出循环
    - DouyinClientError：单视频详情/下载异常 → skip_count +1
    - 其他 Exception：兜底 skip_count +1 + exception traceback
    连续失败阈值：连续 5 个 fail → 跳出循环（避免无效重试）
    """
    new_count = skip_count = 0
    reached_limit = False
    fail_reason = ""
    d = get_db()
    total = len(aweme_ids)
    # #124 连续失败计数器
    consecutive_fails = 0
    CONSECUTIVE_FAIL_THRESHOLD = 5
    processed_count = 0
    for i, aweme_id in enumerate(aweme_ids):
        raise_for_cancel(info)
        # 任务 #589：进度模板只显示当前视频 + 已入库/跳过数 + 用时，
        # 不显示预计剩余时间（视频单条耗时受网络/CDN 抖动影响极大，
        # 估出来的 ETA 经常骗人，不如让用户看实时轮次更踏实）。
        elapsed_s = int(time.time() - (info.start_ts or time.time()))

        # 任务 #589：进度模板 helper，闭包捕获本轮 i/aweme_id/new_count/...
        # 同步调用立即求值，无 late-binding 风险；每轮 def 开销 ~µs 级可忽略。
        def _fmt_progress(stage: str) -> str:
            # B2: 阶段 B 单条模板走模块级 helper + NamedTuple 收口 7 字段，
            # 与 _progress_phase_b 同源（已入库/跳过 都用 skip_count，与 DB 同源）。
            return _progress_phase_b_item(
                _ItemProgressCtx(
                    idx=i + 1, total=total, aweme_id=aweme_id,
                    stage=stage, new_count=new_count,
                    skip_count=skip_count, elapsed_s=elapsed_s,
                ),
            )

        info.progress = _fmt_progress("准备中")
        try:
            # 1. 详情抓取（独立 BrowserActor）— 约 5-10s
            info.progress = _fmt_progress("详情抓取中")
            video = client._fetch_aweme_detail(
                aweme_id, cookie, account_id=account_id, page=page)
            # 2. 单视频统一处理：客户端兜底 → 去重 → 下载 → 内容检测 → 入库
            info.progress = _fmt_progress("下载入库中")
            single = _process_single_video(
                video, category_id, client,
                source="pull", conditions=conditions, cookie=cookie,
                account_id=account_id, info=info,
            )
            action = single["action"]
            vid = video.get("video_id") or ""
            title = (video.get("title") or "")[:50]
            # 统一模板：[拉取] {action_zh} | 视频 {vid[:12]} 标题={title!r}
            # 日志级别按用户视角关键性分级：
            # - filtered / duplicate → DEBUG（高频常态，1000+ 任务刷屏；字幕命中
            #   细节由 _reject_and_cleanup 内部 [过滤] 日志兜底，最全）
            # - failed → INFO（用户视角关键事件，失败原因必现便于排障）
            # - new    → INFO（入库成功是用户最关心的成功事件）
            if action == "filtered":
                # A2: filtered 不再在这里打 INFO，由 _reject_and_cleanup 内部
                # 输出 [过滤] 视频日志（含字幕内容，最全）。本函数只 +1 skip_count。
                skip_count += 1
                logger.debug(
                    "[拉取] 已过滤一条视频：{} | 视频 {} 标题={!r}",
                    single.get("reason"), vid[:12], title,
                )
            elif action == "duplicate":
                # duplicate 是常态：1000+ 任务日志被刷屏，仅 debug 留痕。
                skip_count += 1
                logger.debug(
                    "[拉取] 跳过重复视频：{} | 视频 {} 标题={!r}",
                    single.get("reason"), vid[:12], title,
                )
            elif action == "failed":
                skip_count += 1
                logger.info(
                    "[拉取] 处理失败一条视频：{} | 视频 {} 标题={!r}",
                    single.get("reason"), vid[:12], title,
                )
            elif action == "new":
                new_count += 1
                logger.info(
                    "[拉取] 已入库一条视频 | 视频 {} 标题={!r}",
                    vid[:12], title,
                )
            processed_count += 1
            consecutive_fails = 0
            if new_count >= max_count:
                reached_limit = True
                break
        except LoginInvalidError:
            # #124 登录态失效：账号置 invalid，整轮失败
            fail_reason = "账号登录态失效"
            d.update_by_id("account", account_id, {"status": "invalid"})
            logger.warning("[拉取] 账号登录态失效，账号已置为无效，请重新登录")
            notifier.notify("warn", "task", f"视频任务「{task_name}」账号失效",
                            "请重新登录后再启用任务", action=f"relogin:{account_id}")
            break
        except RiskControlError as e:
            # #124 风控：跳出循环，不再重试
            fail_reason = f"抖音风控拦截：{e}"
            logger.warning("[拉取] 被抖音风控拦截：{}", str(e)[:200])
            break
        except DouyinClientError as e:
            # #124 单视频详情/下载异常
            skip_count += 1
            consecutive_fails += 1
            processed_count += 1
            logger.debug(
                "[拉取] 一条视频处理失败：{}", str(e)[:200],
            )
            if consecutive_fails >= CONSECUTIVE_FAIL_THRESHOLD:
                fail_reason = (
                    f"连续 {consecutive_fails} 个视频处理失败，已停止任务（疑似账号或网络问题）"
                )
                logger.warning("[拉取] 连续失败 {} 个，停止处理后续视频", consecutive_fails)
                break
        except Exception as e:  # noqa: BLE001 单视频失败不影响后续
            skip_count += 1
            consecutive_fails += 1
            processed_count += 1
            logger.debug(
                "[拉取] 一条视频处理异常：{}", str(e)[:200],
            )
            # #124 完整 traceback 记 debug
            logger.exception("[拉取异常trace] video_id={}", aweme_id)
            if consecutive_fails >= CONSECUTIVE_FAIL_THRESHOLD:
                fail_reason = (
                    f"连续 {consecutive_fails} 个视频处理异常，已停止任务"
                )
                break
    # A1: 不在阶段 B 末尾写 INFO（与 _run_pull_round 末尾「任务已结束」重复），
    # 终态汇总统一由 _run_pull_round 输出，阶段 B 仅返回数据。
    return new_count, skip_count, reached_limit, fail_reason, processed_count


# ---------- 分享链接导入（F-03.3） ----------

def import_share_links(share_texts: list[str], category_id: str) -> dict:
    """批量解析分享链接入库（同步执行，逐条结果反馈）。

    任务 #130：与异步分享导入共用 _process_single_video(source='share', conditions=None)，
    行为完全对齐：跳过客户端兜底 + 字幕/人脸过滤 + BGM 同步。
    任务 #132：分享链接兼容视频/音乐，按 _source 分流到 _process_single_video 或
    _process_single_music。
    任务 #111：按分类 type 分流——音乐分类下导入视频链接时，从 video.music 节点
    提取 BGM 入音乐库。

    返回:
        {"results": [{"share_text", "ok", "message", "material_id"}], "success": n, "failed": n}
    """
    d = get_db()
    # 允许 UNCATEGORIZED_ID 作为入库目标（虚拟分类）；其他必须存在且为视频/音乐
    if category_id != UNCATEGORIZED_ID:
        cat = d.query_one(
            "SELECT id, type FROM material_category WHERE id=? AND deleted=0",
            (category_id,))
        if not cat:
            raise ValueError("入库分类不存在")
        if cat["type"] not in ("video", "music"):
            raise ValueError("入库分类类型必须为视频或音乐")
        category_type = cat["type"]
    else:
        # 未分类：根据 type 参数决定实体类型（material_service.import_share_links 没有 type 参数，
        # 调用方应保证入参为 video/music；此处默认 video 兼容既有调用）
        category_type = "video"
    client = get_douyin_client()
    results = []
    for text in share_texts:
        text = text.strip()
        if not text:
            continue
        try:
            resolved = client.resolve_share(text)
            source = resolved.get("_source") or ""
            # 任务 #111 + #132：按分类类型 × 实体类型组合分流
            if category_type == "music" and source == "detail":
                # 音乐分类 + 视频链接 → 提取视频 BGM 入音乐库
                music = resolved.get("music") or {}
                if not music.get("download_url"):
                    raise DouyinClientError(
                        "视频无可用背景音乐（版权受限或无 BGM），无法导入音乐分类")
                result = _process_single_music(music, category_id, client, source="share")
            elif source == "music_detail":
                if category_type != "music":
                    raise DouyinClientError(
                        f"分类类型为 {category_type}，与音乐分享链接不匹配（应在音乐分类下导入）")
                result = _process_single_music(resolved, category_id, client, source="share")
            else:
                if category_type != "video":
                    raise DouyinClientError(
                        f"分类类型为 {category_type}，与视频分享链接不匹配（应在视频分类下导入）")
                result = _process_single_video(
                    resolved, category_id, client,
                    source="share", conditions=None,
                    # 同步分享导入无 worker info，传 None 让 _download_and_ingest
                    # 内部降级 time.sleep（不响应取消信号，调用方控制整体超时）
                    info=None,
                )
            action = result["action"]
            if action == "new":
                results.append({"share_text": text[:50], "ok": True,
                                "message": result["title"], "material_id": result["material_id"]})
            else:
                # filtered / duplicate / failed → 全部归为 failed 给前端
                results.append({"share_text": text[:50], "ok": False,
                                "message": result.get("reason") or "未知失败",
                                "material_id": result.get("material_id")})
        except DouyinClientError as e:
            # 客户端层错误（解析失败/风控/Cookie 失效等）：记录失败原文与原因，便于排障
            logger.warning("[分享导入] 失败 text={!r}：{}", text[:80], e)
            results.append({"share_text": text[:50], "ok": False, "message": str(e), "material_id": None})
        except Exception as e:  # noqa: BLE001
            # 未知异常：全栈记录（下载/入库/探测等环节出错）
            logger.exception("[分享导入] 未知异常 text={!r}", text[:80])
            results.append({"share_text": text[:50], "ok": False, "message": str(e), "material_id": None})
    return {
        "results": results,
        "success": len([r for r in results if r["ok"]]),
        "failed": len([r for r in results if not r["ok"]]),
    }


# ---------- 本地上传（F-03.4） ----------

def upload_files(file_paths: list[str], category_id: str) -> dict:
    """上传本地文件入库：白名单过滤 → 复制到 data/material → MD5 去重 → 探测入库。

    返回:
        {"results": [...], "success": n, "failed": n}
    """
    results = []
    for raw in file_paths:
        results.append(_upload_single_file(raw, category_id))
    return {
        "results": results,
        "success": len([r for r in results if r["ok"]]),
        "failed": len([r for r in results if not r["ok"]]),
    }


def _upload_single_file(raw: str, category_id: str) -> dict:
    """单文件上传入库（供同步上传与异步任务共用）。

    #592 统一：视频 → mp4（c:v copy + 补静音轨）；音频 → mp3（libmp3lame）。
    返回 {"file", "ok", "message", "material_id"}；失败时已用 helper 删 video + cover + 父目录。
    """
    from app.db.utils import new_id
    d = get_db()
    # 允许 UNCATEGORIZED_ID 作为入库目标（虚拟分类）；其他必须存在
    if category_id != UNCATEGORIZED_ID:
        cat = d.query_one("SELECT * FROM material_category WHERE id=? AND deleted=0", (category_id,))
        if not cat:
            raise ValueError("分类不存在")
        material_type = cat["type"]
    else:
        # 未分类入库：按文件扩展名推断类型
        ext_lower = Path(raw).suffix.lower()
        if ext_lower in VIDEO_EXTS:
            material_type = "video"
        elif ext_lower in MUSIC_EXTS:
            material_type = "music"
        else:
            return {"file": Path(raw).name, "ok": False,
                    "message": f"不支持的格式 {ext_lower}", "material_id": None}
    src = Path(raw)
    if not src.exists():
        return {"file": src.name, "ok": False, "message": "文件不存在", "material_id": None}
    ext = src.suffix.lower()
    if (material_type == "video" and ext not in VIDEO_EXTS) or \
       (material_type == "music" and ext not in MUSIC_EXTS):
        return {"file": src.name, "ok": False, "message": f"不支持的格式 {ext}", "material_id": None}
    md5 = _md5_of_file(src)
    dup = _duplicate_check(None, md5)
    if dup:
        return {"file": src.name, "ok": False,
                "message": f"已存在（素材：{dup['title']}）", "material_id": dup["id"]}
    material_id = new_id()
    # #592 目标后缀按类型统一：视频 mp4 / 音频 mp3
    target_ext = ".mp4" if material_type == "video" else ".mp3"
    dest = _build_material_path(material_type, category_id, material_id, src.stem, target_ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    # #592 转码：上传格式 ≠ 目标格式 → ffmpeg 转码（视频 copy 流 + aac / 补静音轨；音频 mp3）
    if ext != target_ext:
        transcoder = _transcode_video_to_mp4 if material_type == "video" else _transcode_audio_to_mp3
        transcoded = transcoder(dest)
        if transcoded is None:
            # 转码失败：删已拷贝的源 + 清空父目录 → 不入库
            _cleanup_material_files(_safe_data_rel(dest))
            return {"file": src.name, "ok": False,
                    "message": f"格式转码失败（{ext}→{target_ext}），素材不入库",
                    "material_id": None}
        # 用转码产物替换 copy 的源文件（统一存 target_ext）
        dest.unlink(missing_ok=True)
        transcoded.rename(dest)
    saved = _probe_and_save(material_id, src.stem, material_type, category_id, dest,
                            "upload", None, None, md5)
    # 视频上传：抽取本地帧作为封面；失败 → 整体不入库（撤回 INSERT + 删 video/cover/目录）
    if material_type == "video":
        duration_ms = saved.get("duration_ms") if saved else None
        cover_rel = _extract_local_cover(dest, material_id, "video",
                                         duration_ms=duration_ms)
        if cover_rel:
            d.execute("UPDATE material SET cover_url=? WHERE id=?", (cover_rel, material_id))
        else:
            # 抽帧失败：撤回入库 → 用 helper 统一清理（删 dest + 抽帧可能生成的 cover + 父目录）
            d.execute("DELETE FROM material WHERE id=?", (material_id,))
            _cleanup_material_files(_safe_data_rel(dest))
            return {"file": src.name, "ok": False,
                    "message": f"封面抽帧失败（duration_ms={duration_ms}ms），素材不入库",
                    "material_id": None}
    return {"file": src.name, "ok": True, "message": "入库成功", "material_id": material_id}


# ---------- 素材列表与维护（F-03.5 / F-03.6 / F-03-R8） ----------

def list_materials(filters: dict, page: int = 1, page_size: int = 20) -> dict:
    """素材分页列表：类型/分类（含子孙）/来源/时长/大小/分辨率/关键词/时间筛选。"""
    d = get_db()
    where = "WHERE m.deleted=0"
    params: list = []
    if filters.get("type"):
        where += " AND m.type=?"
        params.append(filters["type"])
    if filters.get("category_id"):
        # "未分类"为虚拟节点（id='-'，DB 中不存记录）
        if filters["category_id"] == UNCATEGORIZED_ID:
            where += " AND m.category_id=?"
            params.append(UNCATEGORIZED_ID)
        else:
            # 含子孙分类
            cat_ids = _subtree_ids(filters["category_id"])
            where += f" AND m.category_id IN ({','.join('?' * len(cat_ids))})"
            params.extend(cat_ids)
    if filters.get("source_type"):
        where += " AND m.source_type=?"
        params.append(filters["source_type"])
    if filters.get("file_status"):
        where += " AND m.file_status=?"
        params.append(filters["file_status"])
    if filters.get("keyword"):
        where += " AND m.title LIKE ?"
        params.append(f"%{filters['keyword']}%")
    if filters.get("orientation"):
        where += " AND m.orientation=?"
        params.append(filters["orientation"])
    return d.query_page(
        f"""SELECT m.*,
                   COALESCE(c.name, CASE WHEN m.category_id=? THEN '未分类' END) AS category_name
            FROM material m
            LEFT JOIN material_category c ON m.category_id=c.id
            {where} ORDER BY m.create_time DESC""",
        (UNCATEGORIZED_ID, *params), page, page_size)


def _subtree_ids(category_id: str) -> list[str]:
    """分类及全部子孙 ID。"""
    d = get_db()
    ids = [category_id]
    frontier = [category_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = d.query_all(f"SELECT id FROM material_category WHERE parent_id IN ({marks}) AND deleted=0",
                           tuple(frontier))
        frontier = [r["id"] for r in rows]
        ids.extend(frontier)
    return ids


def update_material(material_id: str, title: str | None, category_id: str | None) -> None:
    """素材重命名/移动分类。"""
    fields: dict = {}
    if title is not None:
        fields["title"] = title
    if category_id is not None:
        # 允许 UNCATEGORIZED_ID（合法语义「移动到未分类」）；其他必须存在于 material_category
        if category_id != UNCATEGORIZED_ID:
            d = get_db()
            if not d.query_one(
                    "SELECT id FROM material_category WHERE id=? AND deleted=0",
                    (category_id,)):
                raise ValueError("分类不存在")
        fields["category_id"] = category_id
    if fields:
        get_db().update_by_id("material", material_id, fields)


def delete_material(material_id: str, keep_file: bool = False) -> None:
    """删除素材（引用提示由前端承担；引用处置为素材缺失占位）。

    keep_file=False 时统一删 video + cover + avatar + 清空父目录（#592）。
    """
    d = get_db()
    row = d.query_one(
        "SELECT file_path, cover_url, author_avatar FROM material WHERE id=? AND deleted=0",
        (material_id,))
    if not row:
        raise ValueError("素材不存在")
    # 片段/BGM 引用处置：引用记录保留（生成时校验文件存在性，缺失跳过）
    d.soft_delete_by_id("material", material_id)
    if not keep_file:
        _cleanup_material_files(row["file_path"], row["cover_url"], row["author_avatar"])


def relocate_material(material_id: str, new_abs_path: str) -> None:
    """素材缺失后重新定位文件（F-03-R8）。"""
    d = get_db()
    p = Path(new_abs_path)
    if not p.exists():
        raise ValueError("新路径文件不存在")
    try:
        rel = str(p.relative_to(get_data_dir())).replace("\\", "/")
    except ValueError:
        raise ValueError("新路径必须在 data 目录内（素材统一管理）")
    d.update_by_id("material", material_id, {
        "file_path": rel, "file_status": "normal",
        "file_size": p.stat().st_size})


def full_path_of(material: dict) -> Path:
    """素材相对路径转绝对路径。"""
    return get_data_dir() / material["file_path"]


def get_material(material_id: str) -> dict | None:
    """按 ID 取素材记录（未删除）。"""
    return get_db().query_one("SELECT * FROM material WHERE id=? AND deleted=0", (material_id,))


# ---------- 路径迁移（#364：素材按 ID 子目录聚合） ----------

def migrate_material_paths_to_id_subdir() -> dict:
    """#364 一次性迁移：素材按 id 子目录聚合。

    触发：
    - 视频文件 `material/<type>/<date>/<id>.<ext>` → `material/<type>/<date>/<id>/<id>.<ext>`
    - DB `cover_url` 仍指向 `cache/cover/<id>...` → 搬移到 `material/<type>/<date>/<id>/<id>_cover.<ext>` 并回写
    - DB `author_avatar` 指向 `cache/avatar/<key>...` → 搬移到 `material/<type>/<date>/<id>/<id>_avatar.<ext>` 并回写

    date 取 material.create_time 的 yyyyMMdd 部分。文件不存在时仅更新 DB。
    """
    import os
    d = get_db()
    data_dir = get_data_dir()
    moved = 0
    updated = 0
    for m in d.query_all(
        "SELECT id, type, file_path, cover_url, author_avatar, create_time "
        "FROM material WHERE deleted=0"
    ):
        mid = m["id"]
        mtype = m["type"] or "video"
        date = (m["create_time"] or "")[:10].replace("-", "")

        # 1) 视频文件：旧 `material/<type>/<date>/<id>.<ext>`（无 /<id>/ 子目录层）→ 新
        old_fp = (m["file_path"] or "").replace("\\", "/")
        if old_fp.startswith(f"material/{mtype}/"):
            parts = old_fp.split("/")
            # material/<type>/<date>/<id>.<ext>  ->  parts = [material, type, date, "id.ext"] (len=4)
            if len(parts) == 4 and not parts[3].startswith(f"{mid}/"):
                src = data_dir / old_fp
                if src.is_file():
                    ext = src.suffix
                    new_fp = f"material/{mtype}/{date}/{mid}/{mid}{ext}"
                    dst = data_dir / new_fp
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
                    d.update_by_id("material", mid, {"file_path": new_fp})
                    moved += 1
                    updated += 1

        # 2) cover_url 旧 cache/cover/<id>...<ext> → 新 material/<type>/<date>/<id>/<id>_cover.<ext>
        old_cover = (m["cover_url"] or "").replace("\\", "/")
        if old_cover.startswith("cache/cover/"):
            src = data_dir / old_cover
            if src.is_file():
                ext = src.suffix
                new_cover = f"material/{mtype}/{date}/{mid}/{mid}_cover{ext}"
                dst = data_dir / new_cover
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                d.update_by_id("material", mid, {"cover_url": new_cover})
                moved += 1
                updated += 1

        # 3) author_avatar 旧 cache/avatar/<key>...<ext> → 新 material/<type>/<date>/<id>/<id>_avatar.<ext>
        old_avatar = (m["author_avatar"] or "").replace("\\", "/")
        if old_avatar.startswith("cache/avatar/"):
            src = data_dir / old_avatar
            if src.is_file():
                ext = src.suffix
                new_avatar = f"material/{mtype}/{date}/{mid}/{mid}_avatar{ext}"
                dst = data_dir / new_avatar
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                d.update_by_id("material", mid, {"author_avatar": new_avatar})
                moved += 1
                updated += 1

    return {"moved_files": moved, "updated_rows": updated}
