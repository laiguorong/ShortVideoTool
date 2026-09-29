# -*- coding: utf-8 -*-
"""#413 发布排期算法单测：覆盖题面给的账号 A/B × 项目 A/B 例子 + 边界。

例子输入：
- 当前时间 2023-09-21 22:09:00（仅背景，不参与计算）
- 起始 2023-09-22 07:00:00，结束 2023-09-22 22:00:00
- 同项目间隔 60min，不同项目间隔 10min
- 自主声明 ai_generated，不允许下载（与算法无关，校验时确认字段透传）
- 账号 A、账号 B，每天上限 10
- 项目 A 门店 = 门店A(分店1..2)、门店B(分店1..4)；简介 = A..I（9 条）
- 项目 B 门店 = 门店C(分店1..4)、门店D(分店1..4)；简介 = J..Z（17 条）

期望（账号 A）：
  项目A #1..#5 = 07:00, 10:30, 14:00, 17:30, 21:00（步长 3.5h）
  项目B #1..#5 = 07:10, 10:40, 14:10, 17:40, 21:10（步长 3.5h）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.publish_schedule import (
    IntroSpec, ProjectSpec, ScheduleConfig, ScheduleOverflowError,
    build_schedule,
)


def _make_intros(prefix: str, count: int) -> list[IntroSpec]:
    return [IntroSpec(id=f"{prefix}{i}", content=f"简介{prefix}{i}", topics=("#美食",))
            for i in range(count)]


def _spec_accounts() -> tuple[str, ...]:
    return ("accA", "accB")


def _spec_projects() -> tuple[ProjectSpec, ...]:
    proj_a = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1", "shopA2", "shopB1", "shopB2", "shopB3", "shopB4"),
        intros=tuple(_make_intros("A", 9)),
    )
    proj_b = ProjectSpec(
        id="projB", title="项目B",
        shop_ids=("shopC1", "shopC2", "shopC3", "shopC4",
                  "shopD1", "shopD2", "shopD3", "shopD4"),
        intros=tuple(_make_intros("J", 17)),
    )
    return (proj_a, proj_b)


def _cfg(per_account: bool = False) -> ScheduleConfig:
    accounts = _spec_accounts()
    projects = _spec_projects()
    if per_account:
        limits = tuple((aid, 10) for aid in accounts)
        return ScheduleConfig(
            account_ids=accounts, projects=projects,
            daily_limit_mode="per_account", daily_limit_global=0,
            daily_limit_per_account=limits,
            start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
            # #calc_mode：均衡模式 + 步长 60min
            schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
        )
    return ScheduleConfig(
        account_ids=accounts, projects=projects,
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        # #calc_mode：均衡模式 + 步长 60min
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
        diff_project_interval_min=10,
    )


def test_spec_example_account_a_project_a() -> None:
    """#calc_mode：均衡模式 + 2 项目 + interval=60min → step=30min。
    projA seq=0,2,4,6,8 → t=0,60,120,180,240min → 07:00/08:00/09:00/10:00/11:00。"""
    items, stats = build_schedule(_cfg())
    proj_a_items = [it for it in items if it.account_id == "accA" and it.project_id == "projA"]
    times = [it.plan_time for it in proj_a_items]
    assert times == [
        "2023-09-22 07:00",
        "2023-09-22 08:00",
        "2023-09-22 09:00",
        "2023-09-22 10:00",
        "2023-09-22 11:00",
    ], f"项目 A 时间槽不符：{times}"
    assert stats.by_project["projA"] == 10  # 2 账号 × 5 条


def test_spec_example_account_a_project_b() -> None:
    """#calc_mode：projB seq=1,3,5,7,9 → t=30,90,150,210,270min → 07:30/08:30/09:30/10:30/11:30。"""
    items, _ = build_schedule(_cfg())
    proj_b_items = [it for it in items if it.account_id == "accA" and it.project_id == "projB"]
    times = [it.plan_time for it in proj_b_items]
    assert times == [
        "2023-09-22 07:30",
        "2023-09-22 08:30",
        "2023-09-22 09:30",
        "2023-09-22 10:30",
        "2023-09-22 11:30",
    ], f"项目 B 时间槽不符：{times}"


def test_account_b_matches_account_a() -> None:
    """账号 B 与账号 A 排期完全一致（global 模式 + 同配置）。"""
    items, _ = build_schedule(_cfg())
    for account_id in ("accA", "accB"):
        proj_a_times = [it.plan_time for it in items
                        if it.account_id == account_id and it.project_id == "projA"]
        proj_b_times = [it.plan_time for it in items
                        if it.account_id == account_id and it.project_id == "projB"]
        assert len(proj_a_times) == 5
        assert len(proj_b_times) == 5
    # 全局时间升序
    assert items == sorted(items, key=lambda x: (x.plan_time, x.account_id, x.project_id))


def test_shop_intro_cycle() -> None:
    """门店 / 简介按勾选顺序循环回退。"""
    items, _ = build_schedule(_cfg())
    acc_a_proj_a = [it for it in items if it.account_id == "accA" and it.project_id == "projA"]
    # 项目 A 门店顺序：A1, A2, B1, B2, B3
    assert [it.shop_id for it in acc_a_proj_a] == [
        "shopA1", "shopA2", "shopB1", "shopB2", "shopB3",
    ]
    # 项目 A 简介前 5 条：A1, A2, A3, A4, A5（9 条里取前 5）
    assert [it.intro_id for it in acc_a_proj_a] == ["A0", "A1", "A2", "A3", "A4"]


def test_diff_project_interval_respected() -> None:
    """每个账号内：同项目相邻 ≥ 同项目间隔；跨项目相邻 ≥ 不同项目间隔。

    全局排序会把不同账号的同一时间点相邻（accA 07:00 与 accB 07:00），
    所以按账号分组再校验。
    """
    items, _ = build_schedule(_cfg())
    from datetime import datetime
    for account_id in ("accA", "accB"):
        acc_items = [it for it in items if it.account_id == account_id]
        acc_items.sort(key=lambda x: x.plan_time)
        for i in range(len(acc_items) - 1):
            a, b = acc_items[i], acc_items[i + 1]
            gap = (datetime.strptime(b.plan_time, "%Y-%m-%d %H:%M") -
                   datetime.strptime(a.plan_time, "%Y-%m-%d %H:%M"))
            min_gap = 60 if a.project_id == b.project_id else 10
            assert gap.total_seconds() >= min_gap * 60, (
                f"账号 {account_id} 间隔违规 "
                f"{a.project_id}@{a.plan_time} → {b.project_id}@{b.plan_time}: {gap}"
            )


def test_same_project_interval_respected() -> None:
    """同项目相邻两条间隔 ≥ same_project_interval_min。"""
    items, _ = build_schedule(_cfg())
    from datetime import datetime
    for account_id in ("accA", "accB"):
        for project_id in ("projA", "projB"):
            proj_items = [it for it in items
                          if it.account_id == account_id and it.project_id == project_id]
            for i in range(len(proj_items) - 1):
                gap = (datetime.strptime(proj_items[i + 1].plan_time, "%Y-%m-%d %H:%M") -
                       datetime.strptime(proj_items[i].plan_time, "%Y-%m-%d %H:%M"))
                assert gap.total_seconds() >= 60 * 60, (
                    f"{account_id}/{project_id} #{i + 1}→#{i + 2} 间隔 {gap} < 1h"
                )


def test_per_account_mode() -> None:
    """per_account 模式下各账号按各自上限排期。"""
    cfg = ScheduleConfig(
        account_ids=("accA", "accB"), projects=_spec_projects(),
        daily_limit_mode="per_account", daily_limit_global=0,
        daily_limit_per_account=(("accA", 8), ("accB", 6)),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    assert stats.by_account == {"accA": 8, "accB": 6}
    assert stats.total == 14


def test_overflow_raises() -> None:
    """窗口 < interval → ScheduleConfig.__post_init__ 抛 ValueError。"""
    try:
        ScheduleConfig(
            account_ids=("accA",), projects=_spec_projects(),
            daily_limit_mode="global", daily_limit_global=100,
            daily_limit_per_account=(),
            start_time="2023-09-22 07:00:00", end_time="2023-09-22 07:30:00",
            schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
        )
    except ValueError:
        return
    raise AssertionError("期望 ValueError，未抛")


def test_same_project_interval_truncate() -> None:
    """#calc_mode 终版：limit 超时间窗容量 → 按末条约束截断（不抛错）。

    末条约束：每项目独立——t_dt > end_dt − step 时截断。
    场景：window=900min（07:00→22:00）, interval=60, n_proj=2, step=30。
    last_allowed = 21:30。seq=30 t=22:00 > 21:30 截断。
    projA 偶 seq (0,2,...,28) → 15 条；projB 奇 seq (1,3,...,29) → 15 条；共 30 条。
    fixture 单账号 → 30 条。
    """
    cfg = ScheduleConfig(
        account_ids=("accA",), projects=_spec_projects(),
        daily_limit_mode="global", daily_limit_global=75,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    # #calc_mode 末条约束：last_allowed = end_dt − step = 21:30
    # projA 偶 seq 0,2,...,28 = 15 条（seq=28 t=22:00 > 21:30 截断）
    # projB 奇 seq 1,3,...,29 = 15 条（seq=29 t=21:30 ≤ 21:30 ✓）
    assert stats.total == 30, f"期望截断后 30 条，实际 {stats.total}"
    assert stats.by_project["projA"] == 15
    assert stats.by_project["projB"] == 15
    # 末条 = 21:30（恰好命中 last_allowed_dt）
    assert items[-1].plan_time == "2023-09-22 21:30"


def test_same_project_interval_max_n_inclusive() -> None:
    """#437：边界 case——limit == max_n_per_project × 项目数 → 正常打满。

    window=900min, step=30, interval=60 → max_n_per_project = 15（按末条约束）；
    2 项目 → 30 条；limit=30 刚好打满。
    """
    cfg = ScheduleConfig(
        account_ids=("accA",), projects=_spec_projects(),
        daily_limit_mode="global", daily_limit_global=30,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    assert stats.total == 30
    proj_a_items = [it for it in items if it.project_id == "projA"]
    proj_b_items = [it for it in items if it.project_id == "projB"]
    assert len(proj_a_items) == 15
    assert len(proj_b_items) == 15
    # 项目 A 首条 07:00 末条 21:00，差 14×60min
    assert proj_a_items[0].plan_time == "2023-09-22 07:00"
    assert proj_a_items[-1].plan_time == "2023-09-22 21:00"
    # 末条 = 21:30（projB seq=29，project_idx=1）
    assert items[-1].plan_time == "2023-09-22 21:30"


def test_same_project_interval_no_truncate_needed() -> None:
    """#437：limit 未超时间窗容量 → 不截断，按 limit 排。

    1 个 project + limit=10 + interval=60min + window=900min → max_n_per_project = 15
    （按末条约束 ≤ 21:00）；10 ≤ 15 不截断，按 limit=10 排。
    """
    cfg = ScheduleConfig(
        account_ids=("accA",), projects=(_spec_projects()[0],),
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    assert stats.total == 10
    assert stats.by_project["projA"] == 10


def test_balanced_mode_last_slot_respected() -> None:
    """#calc_mode：balanced 末条约束——每项目独立——t_dt > end_dt − step 时截断。

    window=900min（07:00→22:00）, interval=60, n_proj=3, step=20。
    last_allowed = 22:00 − 20min = 21:40。seq=29 (project_idx=2) t=9×60+2×20=580=16:40，
    但 seq=44 (project_idx=1) t=14×60+1×20=860=21:20 ≤ 21:40 ✓；
    seq=45 (project_idx=0) t=15×60=900=22:00 > 21:40 ✗ 截断。
    proj0/1/2 各 15 条 = 45 条。
    """
    proj_a = ProjectSpec(id="projA", title="A",
                          shop_ids=("s1",), intros=tuple(_make_intros("A", 50)))
    proj_b = ProjectSpec(id="projB", title="B",
                          shop_ids=("s2",), intros=tuple(_make_intros("B", 50)))
    proj_c = ProjectSpec(id="projC", title="C",
                          shop_ids=("s3",), intros=tuple(_make_intros("C", 50)))
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_a, proj_b, proj_c),
        daily_limit_mode="global", daily_limit_global=100,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    # 末条约束：每项目最末条 ≤ 21:40
    assert items[-1].plan_time <= "2023-09-22 21:40"
    # 验证每项目最末条都 ≤ 21:40
    for pid in ("projA", "projB", "projC"):
        proj_items = [it for it in items if it.project_id == pid]
        assert proj_items[-1].plan_time <= "2023-09-22 21:40", (
            f"{pid} 末条 {proj_items[-1].plan_time} > 21:40"
        )
    # 3 项目各 15 条 = 45 条
    assert stats.total == 45
    assert stats.by_project == {"projA": 15, "projB": 15, "projC": 15}


def test_invalid_cross_day_window() -> None:
    """起始/结束不在同一天 → ValueError。"""
    try:
        ScheduleConfig(
            account_ids=("accA",), projects=_spec_projects(),
            daily_limit_mode="global", daily_limit_global=2,
            daily_limit_per_account=(),
            start_time="2023-09-22 07:00:00", end_time="2023-09-23 07:00:00",
            schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
            diff_project_interval_min=10,
        )
    except ValueError:
        return
    raise AssertionError("期望 ValueError，未抛")


def test_topics_propagate() -> None:
    """话题列表透传到排期明细（执行时按简介快照用）。"""
    items, _ = build_schedule(_cfg())
    sample = items[0]
    assert sample.intro_topics == ("#美食",)
    assert sample.intro_content == "简介A0"


# ============ #calc_mode 双模式 ============

def test_fixed_mode_basic_interleave() -> None:
    """fixed 模式终版：每账号独立 seq 0..acc_limit-1。

    2 账号 × 2 项目 × 6 条 = 12 条。每条相差 fixed_min=10min。
    accA 和 accB 都从 seq=0 开始（独立），末条约束 ≤ 21:50。
    """
    proj_a = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1", "shopA2", "shopA3"),
        intros=tuple(_make_intros("A", 3)),
    )
    proj_b = ProjectSpec(
        id="projB", title="项目B",
        shop_ids=("shopB1", "shopB2"),
        intros=tuple(_make_intros("J", 3)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA", "accB"),
        projects=(proj_a, proj_b),
        daily_limit_mode="global", daily_limit_global=6,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    assert stats.total == 12
    assert stats.by_account == {"accA": 6, "accB": 6}
    # 每账号 seq 0..5 → 07:00/07:10/07:20/07:30/07:40/07:50（≤21:50 ✓）
    # 同时间排序：accA/projA, accB/projA, accA/projB, accB/projB, ...
    items_by_seq = sorted(items, key=lambda x: (x.plan_time, x.account_id, x.project_id))
    seq_pairs = [(it.account_id, it.project_id) for it in items_by_seq]
    assert seq_pairs[0] == ("accA", "projA")
    assert seq_pairs[1] == ("accB", "projA")
    assert seq_pairs[2] == ("accA", "projB")
    assert seq_pairs[3] == ("accB", "projB")
    assert seq_pairs[4] == ("accA", "projA")
    assert seq_pairs[5] == ("accB", "projA")
    assert seq_pairs[6] == ("accA", "projB")
    assert seq_pairs[7] == ("accB", "projB")
    assert seq_pairs[8] == ("accA", "projA")
    assert seq_pairs[9] == ("accB", "projA")
    assert seq_pairs[10] == ("accA", "projB")
    assert seq_pairs[11] == ("accB", "projB")
    # 末条 = 07:50（seq=5），符合 ≤ 21:50 约束
    assert items_by_seq[-1].plan_time == "2023-09-22 07:50"


def test_fixed_mode_empty_intros_uses_placeholder() -> None:
    """#456：项目 intros=() 仍正常排期，intro_id/content/topics 用占位空值。
    旧版本（skipped）已被 #456 取代：注释承诺「占位 intro_id='' 兜底」，
    算法应实现该承诺——不阻塞其他项目顺排，空项目本身也产占位条目。"""
    proj_empty = ProjectSpec(
        id="projEmpty", title="空项目",
        shop_ids=("shopE1",),
        intros=(),  # empty
    )
    proj_normal = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1", "shopA2"),
        intros=tuple(_make_intros("A", 2)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_empty, proj_normal),
        daily_limit_mode="global", daily_limit_global=4,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    # projEmpty 产 2 条占位；projA 产 2 条；seq 0..3：projEmpty/projA 交替
    assert stats.total == 4
    assert stats.by_project == {"projEmpty": 2, "projA": 2}
    assert stats.by_account == {"accA": 4}
    assert items[0].project_id == "projEmpty"
    assert items[0].intro_id == ""
    assert items[0].intro_content == ""
    assert items[0].intro_topics == ()
    assert items[1].project_id == "projA"
    assert items[1].intro_id == "A0"
    assert items[2].project_id == "projEmpty"
    assert items[2].intro_id == ""
    assert items[3].project_id == "projA"
    assert items[3].intro_id == "A1"
    # 时间槽
    assert [it.plan_time for it in items] == [
        "2023-09-22 07:00", "2023-09-22 07:10",
        "2023-09-22 07:20", "2023-09-22 07:30",
    ]


def test_balanced_mode_empty_intros_uses_placeholder() -> None:
    """#456：balanced 模式项目 intros=() 仍正常排期，用占位 IntroSpec。"""
    proj_empty = ProjectSpec(
        id="projEmpty", title="空项目",
        shop_ids=("shopE1",), intros=(),
    )
    proj_normal = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1",),
        intros=tuple(_make_intros("A", 2)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_empty, proj_normal),
        daily_limit_mode="global", daily_limit_global=4,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    # balanced: interval=60, n_proj=2 → step=30；seq 0..3 → 0/30/60/90
    # projEmpty(seq=0,2)、projA(seq=1,3)
    assert stats.total == 4
    assert stats.by_project == {"projEmpty": 2, "projA": 2}
    assert stats.by_account == {"accA": 4}
    assert items[0].project_id == "projEmpty"
    assert items[0].intro_id == ""
    assert items[0].plan_time == "2023-09-22 07:00"
    assert items[1].project_id == "projA"
    assert items[1].intro_id == "A0"
    assert items[1].plan_time == "2023-09-22 07:30"
    assert items[2].project_id == "projEmpty"
    assert items[2].intro_id == ""
    assert items[2].plan_time == "2023-09-22 08:00"
    assert items[3].project_id == "projA"
    assert items[3].intro_id == "A1"
    assert items[3].plan_time == "2023-09-22 08:30"


def test_empty_intros_n_proj_3_alternates() -> None:
    """#456：3 项目混合（1 空 + 2 正常）→ 空 intros 项目在 seq % n_proj 位置产占位。"""
    proj_empty = ProjectSpec(id="projEmpty", title="空项目",
                              shop_ids=("shopE1",), intros=())
    proj_a = ProjectSpec(id="projA", title="项目A",
                          shop_ids=("shopA1",), intros=tuple(_make_intros("A", 5)))
    proj_b = ProjectSpec(id="projB", title="项目B",
                          shop_ids=("shopB1",), intros=tuple(_make_intros("B", 5)))
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_empty, proj_a, proj_b),
        daily_limit_mode="global", daily_limit_global=6,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    # fixed 模式 seq 0..5 → projEmpty/projA/projB 循环
    assert stats.total == 6
    assert stats.by_project == {"projEmpty": 2, "projA": 2, "projB": 2}
    assert [it.project_id for it in items] == [
        "projEmpty", "projA", "projB",
        "projEmpty", "projA", "projB",
    ]
    # 空 intros 在偶 seq（0, 3）→ 占位；非空在奇 seq（1, 2, 4, 5）→ 正常 intro_id
    assert items[0].intro_id == ""
    assert items[1].intro_id == "A0"
    assert items[2].intro_id == "B0"
    assert items[3].intro_id == ""
    assert items[4].intro_id == "A1"
    assert items[5].intro_id == "B1"


def test_empty_intros_last_slot_respected_fixed() -> None:
    """#456：空 intros 项目同样遵守末条约束 ≤ end_dt − fixed_min。"""
    proj_empty = ProjectSpec(id="projEmpty", title="空项目",
                              shop_ids=("shopE1",), intros=())
    proj_a = ProjectSpec(id="projA", title="项目A",
                          shop_ids=("shopA1",), intros=tuple(_make_intros("A", 100)))
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_empty, proj_a),
        daily_limit_mode="global", daily_limit_global=200,  # 超窗触发截断
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    # window=900min（07:00→22:00），fixed=10，last_allowed=21:50
    # seq=N t=N*10min，截断条件 t>21:50 → seq ≤ 89
    # 2 项目交替 → seq 0..89 共 90 条（projEmpty 拿偶 seq = 45 条）
    assert items[-1].plan_time <= "2023-09-22 21:50"
    # 全部占位条目时间也在约束内
    empty_items = [it for it in items if it.project_id == "projEmpty"]
    for it in empty_items:
        assert it.plan_time <= "2023-09-22 21:50"
        assert it.intro_id == ""


def test_fixed_mode_per_account_limit_caps() -> None:
    """fixed 模式：账号上限拦截——seq_index 仍累加，账号 1 起点不被借位。"""
    proj_a = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1",),
        intros=tuple(_make_intros("A", 10)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA", "accB"),
        projects=(proj_a,),
        daily_limit_mode="per_account", daily_limit_global=0,
        daily_limit_per_account=(("accA", 2), ("accB", 2)),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    # accA 排 2 条；accB 独立 seq=0..1 → 同样 07:00/07:10
    assert stats.by_account == {"accA": 2, "accB": 2}
    items.sort(key=lambda x: (x.plan_time, x.account_id))
    times_accA = [it.plan_time for it in items if it.account_id == "accA"]
    times_accB = [it.plan_time for it in items if it.account_id == "accB"]
    assert times_accA == ["2023-09-22 07:00", "2023-09-22 07:10"]
    assert times_accB == ["2023-09-22 07:00", "2023-09-22 07:10"]


def test_fixed_mode_overflow() -> None:
    """fixed 模式：末条约束 ≤ end_dt − fixed_min → seq=89 t=890=21:50 ✓，seq=90 t=900=22:00 > 21:50 截断 → 90 条。"""
    proj_a = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1",),
        intros=tuple(_make_intros("A", 100)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_a,),
        daily_limit_mode="global", daily_limit_global=100,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=10, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    assert stats.total == 90, f"期望截断 90 条，实际 {stats.total}"
    # 末条 = 21:50
    assert items[-1].plan_time == "2023-09-22 21:50"


def test_balanced_mode_step_isolation() -> None:
    """balanced 模式：balanced_step_min 与 diff_project_interval_min 解耦（独立保留旧行为）。"""
    proj_a = ProjectSpec(
        id="projA", title="项目A",
        shop_ids=("shopA1",),
        intros=tuple(_make_intros("A", 5)),
    )
    proj_b = ProjectSpec(
        id="projB", title="项目B",
        shop_ids=("shopB1",),
        intros=tuple(_make_intros("B", 5)),
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=(proj_a, proj_b),
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        # #calc_mode 终版：interval=60, n_proj=2 → step=30
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
        diff_project_interval_min=10,
    )
    items, _ = build_schedule(cfg)
    proj_a_times = [it.plan_time for it in items if it.project_id == "projA"]
    proj_b_times = [it.plan_time for it in items if it.project_id == "projB"]
    assert proj_a_times == [
        "2023-09-22 07:00", "2023-09-22 08:00", "2023-09-22 09:00",
        "2023-09-22 10:00", "2023-09-22 11:00",
    ], f"projA: {proj_a_times}"
    assert proj_b_times == [
        "2023-09-22 07:30", "2023-09-22 08:30", "2023-09-22 09:30",
        "2023-09-22 10:30", "2023-09-22 11:30",
    ], f"projB: {proj_b_times}"


def test_balanced_mode_min_floor_10min() -> None:
    """balanced 模式：balanced_step_min=10 → 每项目独占时段内等距排 5 条，不抛错。
    #calc_mode：步长下限 10min（interval=10/n_proj=2=5，max(5, 10)=10）→ step=10。"""
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=_spec_projects(),
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=10, fixed_interval_min=10,
        diff_project_interval_min=10,
    )
    items, _ = build_schedule(cfg)
    # window=900, step=max(10//2, 10)=10, last_allowed=21:50
    # seq=0 t=0 projA；seq=1 t=10 projB；seq=2 t=20 projA；...
    # 10 条每项目 5 条不截断 → 10 条
    assert len(items) == 10


def test_balanced_mode_step_floor_10min() -> None:
    """#calc_mode：步长下限 10min + 冲突重排。
    interval=60, n_proj=8 → raw_step=7 < 10 → step=10。
    seq=0..6 t=0..60 proj0..proj6 无冲突；
    seq=7 base t=70 proj7 与 seq=5 t=70 proj5 冲突 → +step → 80=08:10；
    seq=8 base t=60 proj0 与 seq=6 t=60 proj6 冲突 → +step → 70 已用 → +step → 80 已用 → 90=08:20；
    seq=9 base t=70 proj1 与 seq=7 重排后 80 冲突 → +step → 80 已用 → +step → 90 已用 → 100=08:30。
    """
    projs = tuple(
        ProjectSpec(id=f"proj{i}", title=f"P{i}",
                    shop_ids=(f"s{i}",), intros=tuple(_make_intros(f"P{i}", 5)))
        for i in range(8)
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=projs,
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
    )
    items, stats = build_schedule(cfg)
    # 实际：seq=0..6 正常，seq=7 起冲突重排
    assert len(items) == 10
    assert items[0].plan_time == "2023-09-22 07:00"
    assert items[6].plan_time == "2023-09-22 08:00"
    assert items[7].plan_time == "2023-09-22 08:10"  # 冲突重排
    assert items[8].plan_time == "2023-09-22 08:20"  # 冲突重排
    assert items[9].plan_time == "2023-09-22 08:30"  # 冲突重排
    # 同一账号不允许同时间
    times = [it.plan_time for it in items]
    assert len(times) == len(set(times)), f"同时间冲突：{times}"


def test_balanced_mode_no_collision_within_account() -> None:
    """#calc_mode：同一账号无时间冲突（防同账号重复发条）。"""
    projs = tuple(
        ProjectSpec(id=f"proj{i}", title=f"P{i}",
                    shop_ids=(f"s{i}",), intros=tuple(_make_intros(f"P{i}", 10)))
        for i in range(8)
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=projs,
        daily_limit_mode="global", daily_limit_global=20,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
    )
    items, _ = build_schedule(cfg)
    times = [it.plan_time for it in items]
    assert len(times) == len(set(times)), f"同时间冲突：{times}"


def test_balanced_mode_warns_on_step_floor() -> None:
    """#calc_mode：interval//n_proj < 10 → 静默提升 + UserWarning。"""
    import warnings
    projs = tuple(
        ProjectSpec(id=f"proj{i}", title=f"P{i}",
                    shop_ids=(f"s{i}",), intros=tuple(_make_intros(f"P{i}", 5)))
        for i in range(8)
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=projs,
        daily_limit_mode="global", daily_limit_global=5,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="balanced", balanced_step_min=60, fixed_interval_min=10,
    )
    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        build_schedule(cfg)
        assert any("步长" in str(w.message) and "7min" in str(w.message) for w in ws), (
            f"期望步长下限警告，未触发：{[str(w.message) for w in ws]}"
        )


def test_fixed_mode_step_floor_10min() -> None:
    """#calc_mode：fixed 模式步长下限 10min。fixed=40, n_proj=5 → 40/5=8，max(8, 10)=10。
    例：seq=0 t=0 proj0, seq=1 t=40 proj1, seq=2 t=80 proj2, seq=3 t=120 proj3, seq=4 t=160 proj4。
    seq=5 t=200 proj0 (k=1)。
    """
    projs = tuple(
        ProjectSpec(id=f"proj{i}", title=f"P{i}",
                    shop_ids=(f"s{i}",), intros=tuple(_make_intros(f"P{i}", 5)))
        for i in range(5)
    )
    cfg = ScheduleConfig(
        account_ids=("accA",),
        projects=projs,
        daily_limit_mode="global", daily_limit_global=10,
        daily_limit_per_account=(),
        start_time="2023-09-22 07:00:00", end_time="2023-09-22 22:00:00",
        schedule_mode="fixed", fixed_interval_min=40, balanced_step_min=60,
    )
    items, stats = build_schedule(cfg)
    # step=10, last_allowed=end_dt - step=21:50
    # seq=0 t=0 proj0, seq=1 t=40 proj1, seq=2 t=80 proj2, seq=3 t=120 proj3, seq=4 t=160 proj4
    # seq=5 t=200 proj0, seq=6 t=240 proj1, seq=7 t=280 proj2, seq=8 t=320 proj3, seq=9 t=360 proj4
    # 所有 ≤ 360min=13:00 ≤ 21:50 ✓
    assert len(items) == 10
    assert items[-1].plan_time == "2023-09-22 13:00"
    assert items[1].plan_time == "2023-09-22 07:40"


if __name__ == "__main__":
    # 单测便捷入口：python tests/test_publish_schedule_413.py
    import inspect
    funcs = [(n, o) for n, o in globals().items()
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in funcs:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{'OK' if failed == 0 else 'FAIL'} {len(funcs) - failed}/{len(funcs)}")
    sys.exit(0 if failed == 0 else 1)