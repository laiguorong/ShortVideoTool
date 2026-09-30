# -*- coding: utf-8 -*-
"""视频目录模式多账号不共用同一视频——回归测试。

修复场景：fixed / balanced 模式下，多账号在同目录排期时 k 段重复，
导致同一 video_path 被多个账号的排期条目引用。修复：每个账号 acc_offset 累加。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.publish_schedule import (
    VideoDirProjectSpec,
    VideoDirScheduleConfig,
    build_video_dir_schedule,
)


def _make_dir(dir_id: str, title: str, n_videos: int, n_shops: int = 1) -> VideoDirProjectSpec:
    """构造测试目录 spec：n_videos 个合规视频，1 个门店名。"""
    return VideoDirProjectSpec(
        id=dir_id,
        title=title,
        abs_path=f"/tmp/{dir_id}",  # 测试用路径，不参与逻辑
        video_paths=tuple(f"/video/{dir_id}/v{i}.mp4" for i in range(n_videos)),
        shop_names=tuple(f"门店{dir_id}_{i}" for i in range(n_shops)),
    )


def _paths_by_account(items) -> dict[str, list[str]]:
    """按 account_id 分组 video_path。"""
    out: dict[str, list[str]] = {}
    for it in items:
        out.setdefault(it.account_id, []).append(it.video_path)
    return out


def test_video_dir_fixed_no_overlap_two_accounts():
    """fixed 模式：2 账号 1 目录 4 个视频各发 2 条 → 不共用。

    修复前：账号0/账号1 都从 k=0 起 → 都会用 video[0], video[0]
    修复后：账号0 offset=0 → k=0,1 → video[0], video[1]
            账号1 offset=ceil(2/1)=2 → k=2,3 → video[2], video[3]
    """
    cfg = VideoDirScheduleConfig(
        account_ids=("acc0", "acc1"),
        video_dirs=(_make_dir("dir0", "目录0", n_videos=4),),
        daily_limit_mode="per_account",
        daily_limit_global=0,
        daily_limit_per_account=(("acc0", 2), ("acc1", 2)),
        start_time="2026-09-22 07:00:00",
        end_time="2026-09-22 23:00:00",
        schedule_mode="fixed",
        fixed_interval_min=30,
        manual_intros={"dir0": ("介绍0",)},  # __post_init__ 要求 ≥1 条简介
    )
    items, _ = build_video_dir_schedule(cfg)
    by_acc = _paths_by_account(items)
    # 账号0 用 video[0..1]，账号1 用 video[2..3]，不重叠
    acc0_paths = set(by_acc["acc0"])
    acc1_paths = set(by_acc["acc1"])
    assert acc0_paths == {"/video/dir0/v0.mp4", "/video/dir0/v1.mp4"}, \
        f"账号0 应独占 video[0..1]，实际 {acc0_paths}"
    assert acc1_paths == {"/video/dir0/v2.mp4", "/video/dir0/v3.mp4"}, \
        f"账号1 应独占 video[2..3]，实际 {acc1_paths}"
    assert not (acc0_paths & acc1_paths), f"两账号不应共用视频，实际交集: {acc0_paths & acc1_paths}"


def test_video_dir_fixed_overflow_raises_with_offset():
    """fixed 模式：账号起始 offset 后 k 超长 → 抛 ScheduleVideoShortageError。

    修复前：k=2 总是 video[2]，能跑到第 3 条
    修复后：账号1 offset=2，k=4 超 5 报错——更早发现视频数不够
    """
    from app.services.publish_schedule import ScheduleVideoShortageError
    cfg = VideoDirScheduleConfig(
        account_ids=("acc0", "acc1"),
        video_dirs=(_make_dir("dir0", "目录0", n_videos=5),),
        daily_limit_mode="per_account",
        daily_limit_global=0,
        daily_limit_per_account=(("acc0", 3), ("acc1", 3)),
        start_time="2026-09-22 07:00:00",
        end_time="2026-09-22 23:00:00",
        schedule_mode="fixed",
        fixed_interval_min=30,
        manual_intros={"dir0": ("介绍0",)},
    )
    try:
        build_video_dir_schedule(cfg)
        raise AssertionError("应抛 ScheduleVideoShortageError")
    except ScheduleVideoShortageError as e:
        msg = str(e)
        # 错误信息应包含 acc_offset 提示
        assert "起始 k=" in msg or "acc_offset" in msg.lower() or "k=3" in msg, \
            f"错误信息应提示账号起始 k，实际: {msg}"


def test_video_dir_fixed_three_accounts_one_dir():
    """fixed 模式：3 账号 1 目录 9 个视频各发 3 条 → 9 个视频各用 1 次。

    修复前：3 账号都用 video[0,1,2] → 共用 3 次
    修复后：账号0 k=0..2，账号1 offset=3 k=3..5，账号2 offset=6 k=6..8 → 各 3 个
    """
    cfg = VideoDirScheduleConfig(
        account_ids=("acc0", "acc1", "acc2"),
        video_dirs=(_make_dir("dir0", "目录0", n_videos=9),),
        daily_limit_mode="per_account",
        daily_limit_global=0,
        daily_limit_per_account=(("acc0", 3), ("acc1", 3), ("acc2", 3)),
        start_time="2026-09-22 07:00:00",
        end_time="2026-09-22 23:00:00",
        schedule_mode="fixed",
        fixed_interval_min=30,
        manual_intros={"dir0": ("介绍0",)},
    )
    items, _ = build_video_dir_schedule(cfg)
    by_acc = _paths_by_account(items)
    # 验证 3 个账号 path 互不重叠（每账号 3 条，全 9 条）
    all_paths = []
    for acc in ("acc0", "acc1", "acc2"):
        assert len(by_acc[acc]) == 3, f"{acc} 应有 3 条，实际 {len(by_acc[acc])}"
        all_paths.extend(by_acc[acc])
    assert len(all_paths) == 9
    # 9 个视频路径各不相同
    assert len(set(all_paths)) == 9, f"应 9 个不同 video，实际 {len(set(all_paths))}：{sorted(all_paths)}"


def test_video_dir_balanced_no_overlap_two_accounts():
    """balanced 模式：2 账号 2 目录 6 个视频各发 4 条 → 不共用。

    修复前：账号0/账号1 都从 k=0 起 → dir=0,1,0,1(k=0,0,1,1) + dir=0,1,0,1(k=0,0,1,1)
            → 同 dir 共享 video[0], video[1]
    修复后：账号0 offset=0 → k=0,0,1,1 → video[0], video[0], video[1], video[1]
            账号1 offset=ceil(4/2)=2 → k=2,2,3,3 → video[2], video[2], video[3], video[3]
    """
    cfg = VideoDirScheduleConfig(
        account_ids=("acc0", "acc1"),
        video_dirs=(
            _make_dir("dir0", "目录0", n_videos=6),
            _make_dir("dir1", "目录1", n_videos=6),
        ),
        daily_limit_mode="per_account",
        daily_limit_global=0,
        daily_limit_per_account=(("acc0", 4), ("acc1", 4)),
        start_time="2026-09-22 07:00:00",
        end_time="2026-09-22 23:00:00",
        schedule_mode="balanced",
        balanced_step_min=60,
        manual_intros={"dir0": ("介绍0",), "dir1": ("介绍1",)},
    )
    items, _ = build_video_dir_schedule(cfg)
    by_acc = _paths_by_account(items)
    # 账号0 用 4 条，账号1 用 4 条；同 dir_idx (0 或 1) 内不重叠
    # 收集每账号在每个 dir 用的 video_paths
    by_acc_dir: dict[tuple[str, str], set[str]] = {}
    for it in items:
        by_acc_dir.setdefault((it.account_id, it.project_id), set()).add(it.video_path)
    # 账号0/账号1 在同 dir 的 video 集合应不重叠
    acc0_dir0 = by_acc_dir.get(("acc0", "dir0"), set())
    acc1_dir0 = by_acc_dir.get(("acc1", "dir0"), set())
    overlap = acc0_dir0 & acc1_dir0
    assert not overlap, f"账号0/账号1 在 dir0 共用视频: {overlap}"
    acc0_dir1 = by_acc_dir.get(("acc0", "dir1"), set())
    acc1_dir1 = by_acc_dir.get(("acc1", "dir1"), set())
    overlap = acc0_dir1 & acc1_dir1
    assert not overlap, f"账号0/账号1 在 dir1 共用视频: {overlap}"


# ============ runner ============

def _run_all():
    import inspect
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj) and inspect.isfunction(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed = 0
    failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  [OK] {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed (total {len(funcs)})")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)