# -*- coding: utf-8 -*-
"""发布排期算法（#413 发布管理重设计）。

核心目标：给定账号列表、项目列表（每个含门店、简介）、每天上限、起始/结束时间、
同项目间隔 / 不同项目间隔，输出每个账号 × 每个项目 × 每个门店 × 每条简介 × 每个
时间槽的"待发布明细"（publish_task_item 候选）。

设计要点：
- 纯函数 build_schedule(config) -> list[Item]，无副作用，便于单测
- 时间窗必须同日（start.date == end.date）；跨日不支持
- 容量超出（排不下）抛出 ScheduleOverflowError，让前端向导拦截
- 间隔规则：同项目 ≥ same_project_interval_min；不同项目 ≥ diff_project_interval_min
- 项目内门店 / 简介按"循环回退"使用（不足则从头轮转）
- 每天上限支持两种模式：global（所有账号共用） / per_account（各账号独立）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


class ScheduleOverflowError(ValueError):
    """排期溢出基类——子类型用 category 区分原因，前端按 category 回退到对应 step。

    子类：
    - ScheduleTimeShortageError：时间窗不足
    - ScheduleVideoShortageError：视频目录视频数不足
    """

    category: str = "time"  # 默认时间窗，子类覆盖


class ScheduleTimeShortageError(ScheduleOverflowError):
    """起始/结束时间窗排不下指定数量的视频明细。"""

    category = "time"


class ScheduleVideoShortageError(ScheduleOverflowError):
    """视频目录下合规视频数 < 当前排期所需的第 k 个。"""

    category = "video"


# ---------- 数据结构 ----------

@dataclass(frozen=True)
class IntroSpec:
    """一条视频简介（含话题列表快照）。"""
    id: str
    content: str
    topics: tuple[str, ...]


@dataclass(frozen=True)
class ProjectSpec:
    """一个视频项目（含门店、简介，按勾选顺序）。"""
    id: str
    title: str
    shop_ids: tuple[str, ...]          # 门店顺序敏感
    intros: tuple[IntroSpec, ...]      # 简介顺序敏感


@dataclass(frozen=True)
class ScheduleItem:
    """排期明细（待写入 publish_task_item）。"""
    account_id: str
    project_id: str
    shop_id: str
    intro_id: str
    intro_content: str
    intro_topics: tuple[str, ...]
    plan_time: str                     # #420 yyyy-MM-dd HH:mm（去掉秒数）


@dataclass(frozen=True)
class ScheduleConfig:
    """排期配置（#calc_mode 双模式重设计）。

    模式：
    - 'fixed'：所有账号共用 fixed_interval_min；账号→项目交错顺排（账号0 全项目 → 账号1 全项目 → ...）。
    - 'balanced'：原"等距排期"行为，每项目独占时段内等距；balanced_step_min 直接作步长。

    旧字段 same_project_interval_min / diff_project_interval_min 保留向后兼容读取。
    """
    account_ids: tuple[str, ...]
    projects: tuple[ProjectSpec, ...]
    daily_limit_mode: str              # global / per_account
    daily_limit_global: int            # mode=global 时使用
    daily_limit_per_account: tuple[tuple[str, int], ...]  # mode=per_account 时使用
    start_time: str                    # yyyy-MM-dd HH:mm:ss
    end_time: str                      # yyyy-MM-dd HH:mm:ss（同日）
    # #calc_mode 新字段
    schedule_mode: str = "balanced"    # 'fixed' | 'balanced'
    fixed_interval_min: int = 10       # schedule_mode='fixed' 时使用
    balanced_step_min: int = 60        # schedule_mode='balanced' 时使用（步长）
    # 旧字段保留（向后兼容旧 DB draft）
    same_project_interval_min: int | None = None
    diff_project_interval_min: int | None = None

    def __post_init__(self) -> None:
        if self.daily_limit_mode not in ("global", "per_account"):
            raise ValueError("daily_limit_mode 必须为 global 或 per_account")
        if self.daily_limit_mode == "global" and self.daily_limit_global < 1:
            raise ValueError("daily_limit_global 必须 >= 1")
        # #calc_mode 模式校验
        if self.schedule_mode not in ("fixed", "balanced"):
            raise ValueError(f"schedule_mode 必须为 fixed 或 balanced，当前：{self.schedule_mode!r}")
        if self.schedule_mode == "fixed" and self.fixed_interval_min < 1:
            raise ValueError("固定间隔必须 >= 1 分钟")
        if self.schedule_mode == "balanced" and self.balanced_step_min < 1:
            raise ValueError("均衡间隔必须 >= 1 分钟")
        # #420：兼容无秒（yyyy-MM-dd HH）与带秒两种时间格式（历史草稿/接口透传）
        start = _parse(self.start_time)
        end = _parse(self.end_time)
        if start.date() != end.date():
            raise ValueError("起始/结束时间必须同一天")
        if end <= start:
            raise ValueError("结束时间必须晚于起始时间")
        # #calc_mode 终版：均衡模式要求 window ≥ balanced_step_min（项目内步长）；
        # 否则连 1 条都排不下
        window_min = int((end - start).total_seconds() // 60)
        if self.schedule_mode == "balanced" and window_min < self.balanced_step_min:
            raise ValueError(
                f"均衡模式时间窗 {window_min}min < 均衡间隔 {self.balanced_step_min}min，连 1 条都排不下"
            )
        if not self.account_ids:
            raise ValueError("账号列表不能为空")
        if not self.projects:
            raise ValueError("项目列表不能为空")
        for p in self.projects:
            if not p.shop_ids:
                raise ValueError(f"项目 {p.id} 门店列表不能为空")
            # #456：视频简介允许为空（intros=() 表示不添加视频简介；空校验已删除，
            # 下游 _schedule_for_account_balanced / _schedule_fixed 用占位 intro_id='' 兜底）


@dataclass
class ScheduleStats:
    """排期汇总（便于前端预览展示）。"""
    total: int = 0
    by_account: dict[str, int] = field(default_factory=dict)
    by_project: dict[str, int] = field(default_factory=dict)


# ---------- 核心算法 ----------

def _parse(dt: str) -> datetime:
    """#420：发布时间统一去掉秒数；同时兼容旧格式 yyyy-MM-dd HH:mm:ss（历史草稿/接口透传）。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(dt, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式无法解析：{dt}（应为 yyyy-MM-dd HH:mm）")


def _format(dt: datetime) -> str:
    """#420：plan_time 统一输出 yyyy-MM-dd HH:mm（去掉秒数）。"""
    return dt.strftime("%Y-%m-%d %H:%M")


def _resolve_limits_generic(daily_limit_mode: str,
                            daily_limit_global: int,
                            daily_limit_per_account: tuple[tuple[str, int], ...],
                            account_ids: tuple[str, ...]) -> dict[str, int]:
    """#calc_mode：把 global/per_account 两种模式统一成 {account_id: limit}（项目/视频目录模式共用）。

    global 模式：daily_limit_global 是"每个账号上限"（所有账号共用），每账号都按这个数生成。
    per_account 模式：每个账号独立上限，互不影响。
    """
    if daily_limit_mode == "global":
        # #433 回滚 #430：global 是"每账号上限"原义（非按账号均分）
        # 例：global=75, 2 账号 → 每账号 75, 总 150
        return {aid: daily_limit_global for aid in account_ids}
    per_acc = dict(daily_limit_per_account)
    missing = [aid for aid in account_ids if aid not in per_acc]
    if missing:
        raise ValueError(f"per_account 模式下账号 {missing} 未指定每天上限")
    for aid, lim in per_acc.items():
        if lim < 1:
            raise ValueError(f"账号 {aid} 每天上限必须 >= 1")
    return per_acc


def _resolve_limits(cfg: ScheduleConfig) -> dict[str, int]:
    """项目模式 limits 解析（包装 _resolve_limits_generic 保留原 cfg 入参语义）。"""
    return _resolve_limits_generic(
        cfg.daily_limit_mode, cfg.daily_limit_global, cfg.daily_limit_per_account, cfg.account_ids,
    )


# #calc_mode：步长下限 10min 防风控拒收；旧 interval//n < 10 → 静默提升 + warning
_STEP_MIN_FLOOR_MIN = 10


def _compute_step_with_floor(interval_min: int, n: int, warn_tag: str) -> int:
    """计算步长：max(interval_min // n, 10)；raw < 10 时 warning 静默提升。

    Args:
        interval_min: 原始间隔（fixed 或 balanced 模式输入）
        n: 项目/目录数
        warn_tag: warning 消息前缀（如「均衡」「固定」「视频目录-均衡」），便于排查日志定位
    """
    raw_step = interval_min // n
    step = max(raw_step, _STEP_MIN_FLOOR_MIN)
    if raw_step < _STEP_MIN_FLOOR_MIN:
        import warnings
        warnings.warn(
            f"{warn_tag}步长 {raw_step}min < {_STEP_MIN_FLOOR_MIN}min 下限，已静默提升到 {step}min（n={n}）；"
            f"建议减少项目/目录数或增加间隔",
            UserWarning,
            stacklevel=2,
        )
    return step


def _allocate_per_project(total: int, project_count: int) -> list[int]:
    """把 total 条视频平均分配到 project_count 个项目。

    余数优先分配给"前面"的项目（即按 cfg.projects 顺序的前 N 个）。
    返回长度为 project_count 的列表 [n_p0, n_p1, ...]。
    """
    base, remainder = divmod(total, project_count)
    return [base + (1 if i < remainder else 0) for i in range(project_count)]


# #456：项目 intros=() 时每条用占位 IntroSpec（不阻塞排期；intro_id='' 表示「不添加视频简介」）。
# 单例值对象——IntroSpec frozen=True 不可变，下游可共享引用；前端 PublishDetailDialog 用
# `intro_snapshot ?? '-'` 渲染空占位为 '-'，与正常空话题简介天然区分。
_PLACEHOLDER_INTRO = IntroSpec(id="", content="", topics=())


def _resolve_intro(project_intros: tuple[IntroSpec, ...], k: int) -> IntroSpec:
    """#456：项目 intros 空时返回占位 IntroSpec，否则 round-robin 取第 k 条。"""
    if not project_intros:
        return _PLACEHOLDER_INTRO
    return project_intros[k % len(project_intros)]


def _resolve_shop(project_shop_ids: tuple[str, ...], k: int) -> str:
    """门店 round-robin 取第 k 条。

    shop_ids 是硬约束（__post_init__ L111-114 校验非空），实际不会触发 `return ""`；
    防御性兜底仅供未来若放宽约束时无缝降级，不影响当前正确性。
    """
    if not project_shop_ids:
        return ""
    return project_shop_ids[k % len(project_shop_ids)]


def _schedule_for_account_balanced(account_id: str,
                                    projects: tuple[ProjectSpec, ...],
                                    limit: int,
                                    start_dt: datetime,
                                    end_dt: datetime,
                                    interval_min: int) -> list[ScheduleItem]:
    """为单个账号生成均衡模式排期明细（#calc_mode）。

    算法（用户最终定义）：
    - 步长 = max(interval_min // n_proj, 10)（项目间交错 + 步长下限 10min）
    - seq 全局递增，project_idx = seq % n_proj
    - 每条 plan_time = start_dt + (seq // n_proj) × interval_min + project_idx × step
    - 单账号上限 limit；超过或超窗 → 截断（不抛错）
    - #456：项目 intros=() 用占位 intro_id='' 兜底，仍正常排期
    - 同账号同时间冲突重排：t_dt 已用则按 step 推到下一个空 slot
      （防同账号重复发条；不同账号仍可同时间）
    - K_A=K_B 对称：每项目独立末条约束 ≤ end_dt − step，步长下限 10min 触发 warning

    例：interval=60, n_proj=3, step=20
        seq=0: 0×60 + 0×20 = 0   → 07:00 项目0
        seq=1: 0×60 + 1×20 = 20  → 07:20 项目1
        seq=2: 0×60 + 2×20 = 40  → 07:40 项目2
        seq=3: 1×60 + 0×20 = 60  → 08:00 项目0
        ...
    """
    if interval_min < 1:
        raise ScheduleTimeShortageError(f"均衡间隔必须 ≥ 1min，当前 {interval_min}")
    # #456：不再过滤 intros=() 的项目；空 intros 走占位 IntroSpec
    if not projects:
        return []
    n_proj = len(projects)
    # #calc_mode：步长下限 10min（防止项目数过多导致 step 太小、风控拒收）
    step = _compute_step_with_floor(interval_min, n_proj, "均衡")

    # #calc_mode 末条约束：每项目独立约束——任一条 t_dt > end_dt − step 时截断。
    # 等价于 max_n_per_project = floor((window − step) / interval_min)，
    # 保证每项目最末条 ≤ end_dt − step。
    last_allowed_dt = end_dt - timedelta(minutes=step)

    # #calc_mode：同一账号不允许同一发布时间（防同一账号重复发条）。
    # 当 step × n_proj > interval_min 时，seq 翻页边界 t 倒回 → 按 step 递增重排到空 slot。
    used_times: set[datetime] = set()

    items: list[ScheduleItem] = []
    for seq in range(limit):
        project_idx = seq % n_proj
        t_min = (seq // n_proj) * interval_min + project_idx * step
        # 冲突重排：若与已用同时间，按 step 推到下一个空 slot
        while True:
            t_dt = start_dt + timedelta(minutes=t_min)
            if t_dt > last_allowed_dt:
                break  # 末条约束；超出 → 终止当前 seq
            if t_dt not in used_times:
                break
            t_min += step
        if t_dt > last_allowed_dt:
            break  # 末条约束；超出直接停
        used_times.add(t_dt)
        project = projects[project_idx]
        k = seq // n_proj  # 该项目第几条（用于 round-robin intros/shops）
        intro = _resolve_intro(project.intros, k)
        shop_id = _resolve_shop(project.shop_ids, k)
        items.append(ScheduleItem(
            account_id=account_id,
            project_id=project.id,
            shop_id=shop_id,
            intro_id=intro.id,
            intro_content=intro.content,
            intro_topics=intro.topics,
            plan_time=_format(t_dt),
        ))
    return items


def _schedule_fixed(cfg: ScheduleConfig,
                    start_dt: datetime,
                    end_dt: datetime,
                    limits: dict[str, int]) -> tuple[list[ScheduleItem], ScheduleStats]:
    """#calc_mode 固定模式排期：所有账号共用 fixed_interval_min，每账号独立 seq（对齐视频目录 fixed 模式）。

    算法（用户最终定义）：
    - 步长 = max(fixed_min // n_proj, 10)（项目间交错 + 步长下限 10min）
    - 每账号独立 seq=0..acc_limit-1，project_idx = seq % n_proj（账号0/1 都从 seq=0 开始）
    - 每条 plan_time = start_dt + seq × fixed_interval_min（严格单调递增，天然无同时间冲突）
    - 末条约束：每账号最末条 ≤ end_dt − step
    - 项目 intros=() 时该项目走 #456 占位 IntroSpec（不阻塞其他项目顺排）
    - K_A≠K_B 不对称：seq 推进式 break，末条所在项目可能比前项目少 1 条
      （如 fixed=40, n_proj=2 → projA 12 + projB 11）；需要对称请用 balanced 模式

    例：fixed=10, n_proj=3, acc_limit=4, accounts=2
        账号0 seq=0/1/2/3: 07:00 项目0 / 07:10 项目1 / 07:20 项目2 / 07:30 项目0
        账号1 seq=0/1/2/3: 07:00 项目0 / 07:10 项目1 / 07:20 项目2 / 07:30 项目0
    """
    fixed_min = cfg.fixed_interval_min
    items: list[ScheduleItem] = []
    stats = ScheduleStats()

    # #456：不再过滤 intros=() 的项目；空 intros 走占位 IntroSpec
    if not cfg.projects:
        for account_id in cfg.account_ids:
            stats.by_account[account_id] = 0
        return items, stats

    n_proj = len(cfg.projects)
    # #calc_mode 末条约束：
    # step = max(fixed_min // n_proj, 10)，last_allowed = end_dt − step。
    # 例：fixed=40, n_proj=2 → step=20, last_allowed = end_dt − 20min = 21:40。
    # seq=22 t=21:40 ≤ 21:40 ✓；seq=23 t=22:00 > 21:40 截断 → 末条 = 21:40。
    step = _compute_step_with_floor(fixed_min, n_proj, "固定")
    last_allowed_dt = end_dt - timedelta(minutes=step)

    for account_id in cfg.account_ids:
        acc_limit = limits[account_id]
        produced_for_acc = 0
        for seq in range(acc_limit):
            project_idx = seq % n_proj
            t_dt = start_dt + timedelta(minutes=seq * fixed_min)
            if t_dt > last_allowed_dt:
                break  # 末条约束；超出直接停
            project = cfg.projects[project_idx]
            k = seq // n_proj
            intro = _resolve_intro(project.intros, k)
            shop_id = _resolve_shop(project.shop_ids, k)
            items.append(ScheduleItem(
                account_id=account_id,
                project_id=project.id,
                shop_id=shop_id,
                intro_id=intro.id,
                intro_content=intro.content,
                intro_topics=intro.topics,
                plan_time=_format(t_dt),
            ))
            produced_for_acc += 1
        stats.by_account[account_id] = produced_for_acc

    return items, stats


def build_schedule(cfg: ScheduleConfig) -> tuple[list[ScheduleItem], ScheduleStats]:
    """执行排期，返回 (明细列表, 汇总)。

    明细按"全局 plan_time 升序"排列，便于写入数据库后直接按 plan_time 取用。

    #calc_mode：根据 schedule_mode 分发到 fixed 或 balanced 实现。
    - balanced：seq 单账号内全局递增，project_idx = seq % n_proj，步长 = interval // n_proj（≥ 10）
    - fixed：每账号独立 seq（账号0/1 都从 seq=0 开始），project_idx = seq % n_proj，步长 = interval（≥ 10）
    """
    start_dt = _parse(cfg.start_time)
    end_dt = _parse(cfg.end_time)
    limits = _resolve_limits(cfg)

    if cfg.schedule_mode == "fixed":
        all_items, stats = _schedule_fixed(cfg, start_dt, end_dt, limits)
    else:
        all_items: list[ScheduleItem] = []
        stats = ScheduleStats()
        for account_id in cfg.account_ids:
            items = _schedule_for_account_balanced(
                account_id=account_id,
                projects=cfg.projects,
                limit=limits[account_id],
                start_dt=start_dt,
                end_dt=end_dt,
                interval_min=cfg.balanced_step_min,
            )
            all_items.extend(items)
            stats.by_account[account_id] = len(items)

    # 全局按时间升序
    all_items.sort(key=lambda x: (x.plan_time, x.account_id, x.project_id))

    stats.total = len(all_items)
    for it in all_items:
        stats.by_project[it.project_id] = stats.by_project.get(it.project_id, 0) + 1

    # #433 + #437 防御：实际生成的明细数 ≤ 各账号上限之和
    # global 模式：每账号都按 global 生成 → expected = global × n_acc
    # per_account 模式：每账号按各自 limit → expected = sum(per_account.limits)
    # #437：当 limit 超时间窗容量时按 max_n_per_project 截断，actual ≤ expected
    if cfg.daily_limit_mode == "global":
        expected_total = cfg.daily_limit_global * len(cfg.account_ids)
    else:
        expected_total = sum(lim for _, lim in cfg.daily_limit_per_account)
    if stats.total > expected_total:
        raise ScheduleOverflowError(
            f"排期后总 {stats.total} > 上限 {expected_total}（算法异常：by_account={stats.by_account}）"
        )
    for aid, lim in limits.items():
        if stats.by_account.get(aid, 0) > lim:
            raise ScheduleOverflowError(
                f"账号 {aid} 排期 {stats.by_account.get(aid, 0)} > 上限 {lim}（算法异常）"
            )
    return all_items, stats

# ---------- #418 视频目录模式独立排期 ----------

@dataclass(frozen=True)
class VideoDirProjectSpec:
    """视频目录模式：一个目录 = 一个虚拟项目（按顺序顺排视频文件）。"""
    id: str
    title: str
    abs_path: str                 # 目录绝对路径
    video_paths: tuple[str, ...]  # 合规视频文件绝对路径（按扫描顺序）
    shop_names: tuple[str, ...]   # 手动输入的门店名称列表（顺序循环）


@dataclass(frozen=True)
class VideoDirScheduleItem:
    """视频目录模式排期明细。"""
    account_id: str
    project_id: str               # = dir.id
    video_path: str               # 视频文件绝对路径
    shop_name: str                # 手动输入门店名（poi_id 留空，发布时按名称模糊匹配）
    intro_content: str            # #292：手动输入视频简介（按视频顺序轮转）
    plan_time: str


@dataclass(frozen=True)
class VideoDirScheduleConfig:
    """视频目录模式排期配置（#calc_mode 字段对齐 ScheduleConfig）。

    模式：
    - 'fixed'：所有账号共用 fixed_interval_min；每账号独立 seq；目录→视频 顺排。
    - 'balanced'：与 _schedule_for_account_balanced 算法对齐——step = max(interval // n_dirs, 10)，
      目录间交错偏移 = step。两模式同配置生成一致 plan_time 序列。

    字段命名与 ScheduleConfig 完全一致，便于上层 service 统一构造/落库。
    """
    account_ids: tuple[str, ...]
    video_dirs: tuple[VideoDirProjectSpec, ...]
    daily_limit_mode: str
    daily_limit_global: int
    daily_limit_per_account: tuple[tuple[str, int], ...]
    start_time: str
    end_time: str
    # #calc_mode：三字段与 ScheduleConfig 对齐
    schedule_mode: str = "fixed"        # video_dir 主走 fixed 模式（用户场景）；balanced 兼容
    fixed_interval_min: int = 10
    balanced_step_min: int = 60
    # 视频简介-按项目一一对应：key=dir.id，value=该目录下的简介元组；按各目录的视频数独立轮转
    manual_intros: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.daily_limit_mode not in ("global", "per_account"):
            raise ValueError("daily_limit_mode 必须为 global 或 per_account")
        if self.daily_limit_mode == "global" and self.daily_limit_global < 1:
            raise ValueError("daily_limit_global 必须 >= 1")
        if self.schedule_mode not in ("fixed", "balanced"):
            raise ValueError(f"schedule_mode 必须为 fixed 或 balanced，当前：{self.schedule_mode!r}")
        if self.schedule_mode == "fixed" and self.fixed_interval_min < 1:
            raise ValueError("固定间隔必须 >= 1 分钟")
        if self.schedule_mode == "balanced" and self.balanced_step_min < 1:
            raise ValueError("均衡间隔必须 >= 1 分钟")
        start = _parse(self.start_time)
        end = _parse(self.end_time)
        if start.date() != end.date():
            raise ValueError("起始/结束时间必须同一天")
        if end <= start:
            raise ValueError("结束时间必须晚于起始时间")
        if not self.account_ids:
            raise ValueError("账号列表不能为空")
        if not self.video_dirs:
            raise ValueError("视频目录列表不能为空")
        for d in self.video_dirs:
            if not d.video_paths:
                raise ValueError(f"目录 {d.title} 下无合规视频")
            if not d.shop_names:
                raise ValueError(f"目录 {d.title} 门店列表不能为空（手动输入）")
            # 视频简介-按项目一一对应：每个目录必须有 ≥1 条简介
            intros_for_dir = self.manual_intros.get(d.id) or ()
            if not intros_for_dir:
                raise ValueError(f"目录 {d.title} 必须至少输入 1 条视频简介")


@dataclass
class VideoDirScheduleStats:
    total: int = 0
    by_account: dict[str, int] = field(default_factory=dict)
    by_directory: dict[str, int] = field(default_factory=dict)


def _schedule_video_dir_balanced(account_id: str,
                                 video_dirs: tuple[VideoDirProjectSpec, ...],
                                 manual_intros: dict[str, tuple[str, ...]],
                                 limit: int,
                                 start_dt: datetime,
                                 end_dt: datetime,
                                 interval_min: int,
                                 acc_offset: int = 0) -> list[VideoDirScheduleItem]:
    """#418 视频目录模式 - 均衡模式（与 _schedule_for_account_balanced 算法对齐：两模式同配置应生成一致 plan_time）。

    算法对齐（#calc_mode 对齐项目模式）：
    - step = max(interval_min // n_dirs, 10)（_compute_step_with_floor 防项目数过多导致 step 太小）
    - seq 单账号内全局递增，dir_idx = seq % n_dirs
    - 每条 plan_time = start_dt + (seq // n_dirs) × interval_min + dir_idx × step
    - 单账号上限 limit；超窗 → 截断（不抛错）
    - 末条约束：每目录独立——任一条 t_dt > end_dt − step 时截断
    - 同账号冲突重排（同 _schedule_for_account_balanced）：t_dt 已被占则按 step 推到下一个空 slot
    - 视频简介-按项目一一对应：取该目录专属的简介池按 k 轮转
    - 门店按 k 轮转
    - 视频文件取该目录下第 k 个（k 超长则抛 ScheduleOverflowError，由前端拦截）
    - #fix-video-dedup：acc_offset 让多账号在同一目录的 k 段错开，避免共用同一视频文件

    例：interval_min=60, n_dirs=2 → step=30
        seq=0: 0×60 + 0×30 = 0    → 07:00 dir0
        seq=1: 0×60 + 1×30 = 30   → 07:30 dir1
        seq=2: 1×60 + 0×30 = 60   → 08:00 dir0
        seq=3: 1×60 + 1×30 = 90   → 08:30 dir1
        （与项目模式 _schedule_for_account_balanced 完全一致）
    """
    if interval_min < 1:
        raise ScheduleTimeShortageError(f"均衡间隔必须 ≥ 1min，当前 {interval_min}")
    if not video_dirs:
        return []
    n_dirs = len(video_dirs)
    step = _compute_step_with_floor(interval_min, n_dirs, "视频目录-均衡")
    last_allowed_dt = end_dt - timedelta(minutes=step)

    items: list[VideoDirScheduleItem] = []
    used_times: set[datetime] = set()
    for seq in range(limit):
        dir_idx = seq % n_dirs
        t_min = (seq // n_dirs) * interval_min + dir_idx * step
        # 同账号冲突重排（同 _schedule_for_account_balanced）；内层 while 检查 last_allowed 兜底
        while True:
            t_dt = start_dt + timedelta(minutes=t_min)
            if t_dt > last_allowed_dt:
                break  # 末条约束；超出 → 终止当前 seq
            if t_dt not in used_times:
                break
            t_min += step
        if t_dt > last_allowed_dt:
            break  # 末条约束；超出直接停
        used_times.add(t_dt)
        d = video_dirs[dir_idx]
        # #fix-video-dedup：acc_offset 让多账号在同目录占不同 k 段，避免共用视频
        k = seq // n_dirs + acc_offset
        # 视频数耗尽：直接抛错，让前端提示用户调整数量/换目录（避免重复排期同文件导致多条明细指向同一视频）
        if k >= len(d.video_paths):
            raise ScheduleVideoShortageError(
                f"目录「{d.title}」仅有 {len(d.video_paths)} 个合规视频，"
                f"当前账号需发布 {limit} 条、按 {n_dirs} 个目录平均分配，"
                f"本账号起始 k={acc_offset}、本目录需 k={k + 1}。"
                f"请减少每账号发布数或增加目录内视频。"
            )
        vp = d.video_paths[k]
        # 门店 / 简介轮转（__post_init__ 已强制非空）
        shop_name = d.shop_names[k % len(d.shop_names)]
        intros_for_dir = manual_intros.get(d.id) or ()
        intro_content = intros_for_dir[k % len(intros_for_dir)]
        items.append(VideoDirScheduleItem(
            account_id=account_id,
            project_id=d.id,
            video_path=vp,
            shop_name=shop_name,
            intro_content=intro_content,
            plan_time=_format(t_dt),
        ))
    return items


def build_video_dir_schedule(cfg: VideoDirScheduleConfig) -> tuple[list[VideoDirScheduleItem], VideoDirScheduleStats]:
    """#418 视频目录模式排期（#calc_mode 重写对齐 ScheduleConfig）。

    与 build_schedule 同构：
    - balanced：每账号独立 seq（账号0/1 都从 seq=0 开始）；目录间交错；步长 = step_min（用户配置）
    - fixed：每账号独立 seq；目录间交错；步长 = max(fixed_min // n_dirs, 10)

    视频简介 / 门店：按 k（seq // n_dirs）在该目录专属池内轮转。
    视频文件：按 k 取该目录下第 k 个（k 超长则复用尾条——业务上表示该目录视频用尽但仍继续顺排）。
    """
    start_dt = _parse(cfg.start_time)
    end_dt = _parse(cfg.end_time)
    limits = _resolve_video_dir_limits(cfg)

    if cfg.schedule_mode == "balanced":
        items: list[VideoDirScheduleItem] = []
        stats = VideoDirScheduleStats()
        # #fix-video-dedup：累加 acc_offset 让每账号在每个目录占独立 k 段
        n_dirs = len(cfg.video_dirs)
        acc_offset = 0
        for account_id in cfg.account_ids:
            account_items = _schedule_video_dir_balanced(
                account_id=account_id,
                video_dirs=cfg.video_dirs,
                manual_intros=cfg.manual_intros,
                limit=limits[account_id],
                start_dt=start_dt,
                end_dt=end_dt,
                interval_min=cfg.balanced_step_min,
                acc_offset=acc_offset,
            )
            items.extend(account_items)
            stats.by_account[account_id] = len(account_items)
            # 下一个账号的 acc_offset：本账号最多占 ceil(limit / n_dirs) 段
            if limits[account_id] > 0 and n_dirs > 0:
                acc_offset += (limits[account_id] + n_dirs - 1) // n_dirs
    else:
        # fixed：单函数生成（与 _schedule_fixed 同构，seq 跨账号累加）
        items, stats = _schedule_video_dir_fixed_inner(
            cfg.account_ids, cfg.video_dirs, limits, start_dt, end_dt, cfg.fixed_interval_min, cfg.manual_intros,
        )

    # 全局按时间升序（plan_time, account_id, project_id）
    items.sort(key=lambda x: (x.plan_time, x.account_id, x.project_id))
    stats.total = len(items)
    # 重建 by_directory（_schedule_video_dir_fixed_inner 已填；balanced 走单账号未填，补一遍）
    if cfg.schedule_mode == "balanced":
        for it in items:
            stats.by_directory[it.project_id] = stats.by_directory.get(it.project_id, 0) + 1
    return items, stats


# ---------- helpers（模块内私有，避免污染 dataclass 接口） ----------

def _resolve_video_dir_limits(cfg: VideoDirScheduleConfig) -> dict[str, int]:
    """#calc_mode：视频目录模式 limits 解析（包装 _resolve_limits_generic 复用项目版逻辑）。"""
    return _resolve_limits_generic(
        cfg.daily_limit_mode, cfg.daily_limit_global, cfg.daily_limit_per_account, cfg.account_ids,
    )


def _schedule_video_dir_fixed_inner(account_ids: tuple[str, ...],
                                    video_dirs: tuple[VideoDirProjectSpec, ...],
                                    limits: dict[str, int],
                                    start_dt: datetime,
                                    end_dt: datetime,
                                    fixed_min: int,
                                    manual_intros: dict) -> tuple[list[VideoDirScheduleItem], VideoDirScheduleStats]:
    """fixed 模式具体实现（#calc_mode：对齐项目 _schedule_fixed —— 每账号独立 seq，账号0/1 都从 seq=0 开始）。

    算法：
    - 步长 = max(fixed_min // n_dirs, 10)（_compute_step_with_floor）
    - 每账号独立 seq=0..acc_limit-1，dir_idx = seq % n_dirs
    - 每条 plan_time = start_dt + seq × fixed_min（严格单调递增，天然无同时间冲突）
    - 末条约束：每账号最末条 ≤ end_dt − step
    - 视频文件 / 门店 / 简介：按 k（k = seq // n_dirs + acc_offset）在该目录专属池轮转
      - #fix-video-dedup：acc_offset 让多账号在同一目录占不同 k 段，避免共用同一视频
    - k 超长抛 ScheduleOverflowError

    例：fixed_min=10, n_dirs=3, accounts=2, acc_limit=4
        账号0: 07:00/07:10/07:20/07:30（k=0/0/0/1 → video[0]/[0]/[0]/[1]）
        账号1: 07:00/07:10/07:20/07:30（k=2/2/2/3 → video[2]/[2]/[2]/[3]，acc_offset=2）
        每账号同起点 + 不同视频段 → 多账号不共用同一视频
    """
    if fixed_min < 1:
        raise ScheduleTimeShortageError(f"固定间隔必须 ≥ 1min，当前 {fixed_min}")
    if not video_dirs:
        return [], VideoDirScheduleStats()
    n_dirs = len(video_dirs)
    step = _compute_step_with_floor(fixed_min, n_dirs, "视频目录-固定")
    last_allowed_dt = end_dt - timedelta(minutes=step)

    items: list[VideoDirScheduleItem] = []
    stats = VideoDirScheduleStats()
    # #fix-video-dedup：累加 acc_offset 让每账号在每个目录占独立 k 段，避免多账号共用同一视频
    acc_offset = 0
    for account_id in account_ids:
        limit = limits[account_id]
        produced_for_acc = 0
        for seq in range(limit):
            dir_idx = seq % n_dirs
            t_dt = start_dt + timedelta(minutes=seq * fixed_min)
            if t_dt > last_allowed_dt:
                break
            d = video_dirs[dir_idx]
            # acc_offset 让同 dir_idx 的多账号 k 错开（账号0: 0..N，账号1: M..M+N）
            k = seq // n_dirs + acc_offset
            # 视频数耗尽：直接抛错，让前端提示用户调整数量/换目录（避免重复排期同文件导致多条明细指向同一视频）
            if k >= len(d.video_paths):
                raise ScheduleVideoShortageError(
                    f"目录「{d.title}」仅有 {len(d.video_paths)} 个合规视频，"
                    f"当前账号起始 k={acc_offset}、本目录需 k={k + 1}。"
                    f"请减少每账号发布数或增加目录内视频。"
                )
            vp = d.video_paths[k]
            shop_name = d.shop_names[k % len(d.shop_names)]
            intros_for_dir = manual_intros.get(d.id) or ()
            intro_content = intros_for_dir[k % len(intros_for_dir)]
            items.append(VideoDirScheduleItem(
                account_id=account_id,
                project_id=d.id,
                video_path=vp,
                shop_name=shop_name,
                intro_content=intro_content,
                plan_time=_format(t_dt),
            ))
            produced_for_acc += 1
            stats.total += 1
            stats.by_account[account_id] = stats.by_account.get(account_id, 0) + 1
            stats.by_directory[d.id] = stats.by_directory.get(d.id, 0) + 1
        # 下一个账号的 acc_offset：本账号最多占 ceil(limit / n_dirs) 段
        if limit > 0:
            acc_offset += (limit + n_dirs - 1) // n_dirs
    return items, stats
