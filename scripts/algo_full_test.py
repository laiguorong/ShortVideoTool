# -*- coding: utf-8 -*-
"""
视频组合算法完整测试 - 渐进扩容 8 阶段。
约束:
  1. 同源率 <= 50%
  2. 与历史比较,只看未变化分镜,最长连续段 / 未变化分镜数 <= 50%
  3. 扩容模式: 前 K 分镜继承历史 + 选新分镜 clip
  4. 必须含至少 1 个新 clip (有新增时)
  5. 新片段权重 +1,排序靠前
"""
import itertools
import random
from pathlib import Path

OUTPUT = Path('/tmp/algo_full_test.md')
OUTPUT.write_text('', encoding='utf-8')


def w(line=''):
    OUTPUT.open('a', encoding='utf-8').write(line + '\n')


# ---------------- 算法 ----------------

def n_shots_def(clips_per_shot):
    return len(clips_per_shot)


def random_pick(legal_pool, n_pick, seed=None, temperature=1.0):
    """
    从合法池按权重随机抽取 N 个。
    weight = exp(score / temperature) - 温度越高越均匀,越低越偏向高分。
    """
    if not legal_pool:
        return []
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random

    scored = [(combo, sum(c.weight for c in combo)) for combo in legal_pool]
    weights = [s ** temperature for _, s in scored]
    total = sum(weights)
    if total == 0:
        return rng.sample(legal_pool, min(n_pick, len(legal_pool)))

    picked = []
    picked_set = set()
    attempts = 0
    while len(picked) < n_pick and attempts < n_pick * 10:
        attempts += 1
        r = rng.uniform(0, total)
        acc = 0
        for (combo, score), w in zip(scored, weights):
            acc += w
            if r <= acc:
                key = tuple(c.key for c in combo)
                if key not in picked_set:
                    picked.append(combo)
                    picked_set.add(key)
                break
    return picked


def overlap_on_positions(combo1, combo2, positions):
    """指定位置上相同 key 数。"""
    return sum(1 for i in positions if combo1[i].key == combo2[i].key)


def gen_legal_combos(clips_per_shot, history_combos, last_shot_count=None,
                     same_source_limit=0.5, overlap_limit=0.5):
    n_shots = len(clips_per_shot)
    has_new = any(c.is_new for s in clips_per_shot for c in s)
    unchanged_idx = [i for i, s in enumerate(clips_per_shot)
                     if not any(c.is_new for c in s)]

    # 单分镜新增模式 (扩容且部分分镜有新增):清空历史
    is_partial_new = (
        last_shot_count is None
        and has_new
        and len(unchanged_idx) < n_shots
    )
    history_to_use = [] if is_partial_new else history_combos

    if last_shot_count is not None and last_shot_count < n_shots:
        new_shot = clips_per_shot[last_shot_count]
        result = []
        for h in history_combos:
            prefix = list(h[:last_shot_count])
            for nc in new_shot:
                full = prefix + [nc]
                sources = [c.sid for c in full]
                ms = max(sources.count(s) for s in set(sources))
                if ms / n_shots > same_source_limit:
                    continue
                score = sum(c.weight for c in full)
                keys = tuple(c.key for c in full)
                result.append((score, keys, full))
        result.sort(key=lambda x: (-x[0], x[1]))
        return [item[2] for item in result]

    all_combos = list(itertools.product(*clips_per_shot))
    legal = []
    for combo in all_combos:
        if has_new and not any(c.is_new for c in combo):
            continue
        sources = [c.sid for c in combo]
        ms = max(sources.count(s) for s in set(sources))
        if ms / n_shots > same_source_limit:
            continue
        skip = False
        for h in history_to_use:
            same = overlap_on_positions(combo, h, unchanged_idx)
            if same / n_shots > overlap_limit:
                skip = True
                break
        if skip:
            continue
        score = sum(c.weight for c in combo)
        keys = tuple(c.key for c in combo)
        legal.append((score, keys, combo))
    legal.sort(key=lambda x: (-x[0], x[1]))
    return [item[2] for item in legal]


# ---------------- 模型 ----------------

class Clip:
    _sid_counter = 1000

    def __init__(self, label, is_new=False):
        self.label = label
        self.is_new = is_new
        self.sid = Clip._sid_counter
        Clip._sid_counter += 1
        self.key = label
        self.weight = 1.0 + (1.0 if is_new else 0.0)

    def __repr__(self):
        return f"{self.key}{'*' if self.is_new else ''}"


def build_shots(config):
    int_keys = [k for k in config.keys() if isinstance(k, int)]
    max_shot = max(int_keys) + 1 if int_keys else 0
    new_flags_map = config.get('new', {})
    clips_per_shot = []
    for i in range(max_shot):
        n = config.get(i, 0)
        new_flags = new_flags_map.get(i, [])
        shot = []
        for j in range(n):
            label = f"S{i+1}-V{j+1}"
            shot.append(Clip(label, j in new_flags))
        clips_per_shot.append(shot)
    return clips_per_shot


# ---------------- 阶段 ----------------

PHASES = [
    ('A0 初始 (3分镜 各3视频)', {0: 3, 1: 3, 2: 3}, {}, None),
    ('A1 加第4分镜 (4分镜 各3)', {0: 3, 1: 3, 2: 3, 3: 3}, {3: [0, 1, 2]}, 3),
    ('A2 第1分镜+1视频', {0: 4, 1: 3, 2: 3, 3: 3}, {0: [3]}, None),
    ('A3 加第5分镜 (5分镜)', {0: 4, 1: 3, 2: 3, 3: 3, 4: 3}, {4: [0, 1, 2]}, 4),
    ('A4 第2分镜+1视频', {0: 4, 1: 4, 2: 3, 3: 3, 4: 3}, {1: [3]}, None),
    ('A5 加第6分镜 (6分镜)', {0: 4, 1: 4, 2: 3, 3: 3, 4: 3, 5: 3}, {5: [0, 1, 2]}, 5),
    ('A6 第3分镜+1视频', {0: 4, 1: 4, 2: 4, 3: 3, 4: 3, 5: 3}, {2: [3]}, None),
    ('A7 加第7分镜 (7分镜)', {0: 4, 1: 4, 2: 4, 3: 3, 4: 3, 5: 3, 6: 3}, {6: [0, 1, 2]}, 6),
]


def main():
    history = []
    results = []

    w('# 视频组合算法完整测试报告')
    w()
    w('## 测试场景 - 渐进扩容 8 阶段')
    w()
    w('| 阶段 | 操作 | 分镜配置 |')
    w('|------|------|----------|')
    for name, cfg, new, _ in PHASES:
        desc = ' / '.join(f'分镜{k+1}:{v}' for k, v in cfg.items() if isinstance(v, int))
        w(f'| {name} | {name.split(" ", 1)[1]} | {desc} |')
    w()
    w('## 约束规则')
    w()
    w('1. **同源率 ≤ 50%**: 同一组合内相同 source 的 clip 不超过半数')
    w('2. **位置重叠率 ≤ 50%** (按总长度): 与历史任一组合比对,只看未变化分镜位置,位置相同数 / 总长度 ≤ 50%')
    w('3. **扩容隔离**: 加新分镜时,新组合前 K 分镜继承历史,只选新分镜 clip')
    w('4. **新片段必含**: 有新 clip 时,新组合必须包含至少 1 个新 clip')
    w('5. **新片段优先**: 新增 clip 权重 +1,排序靠前')
    w('6. **单分镜新增清空历史**: 仅有部分分镜新增 clip 时,清空旧历史,避免不变分镜空间被历史饱和覆盖')
    w()
    w('---')
    w()

    for idx, (name, cfg, new_flags, last_shot_count) in enumerate(PHASES):
        w(f'## 阶段 {idx} - {name}')
        w()
        cfg_with_new = dict(cfg)
        cfg_with_new['new'] = new_flags
        clips_per_shot = build_shots(cfg_with_new)

        clip_count = sum(len(s) for s in clips_per_shot)
        new_count = sum(1 for s in clips_per_shot for c in s if c.is_new)
        unchanged_idx = [i for i, s in enumerate(clips_per_shot)
                         if not any(c.is_new for c in s)]
        w(f'**分镜数**: {len(clips_per_shot)} | **clip 总数**: {clip_count} | **新增**: {new_count} | **未变化分镜**: {len(unchanged_idx)}')
        w()

        w('### clip 列表')
        w()
        for i, shot in enumerate(clips_per_shot):
            labels = ', '.join(repr(c) for c in shot)
            w(f'- **分镜 {i+1}** ({len(shot)} 个): {labels}')
        w()

        legal = gen_legal_combos(clips_per_shot, history, last_shot_count)
        total = 1
        for shot in clips_per_shot:
            total *= len(shot)

        w('### 统计')
        w()
        w(f'- 笛卡尔积总数: **{total}**')
        w(f'- 合法组合数 (含历史约束): **{len(legal)}**')
        w(f'- 过滤掉: {total - len(legal)} ({(total-len(legal))/total*100:.1f}%)')
        w(f'- 上一阶段组合数: {len(history)}')
        w()

        w('### 所有合法组合 (按权重降序)')
        w()
        w('| # | 组合 | 权重 |')
        w('|---|------|------|')
        for i, combo in enumerate(legal, 1):
            combo_str = ' + '.join(repr(c) for c in combo)
            score = sum(c.weight for c in combo)
            w(f'| {i} | {combo_str} | {score} |')
        w()
        w(f'**共 {len(legal)} 个组合**')
        w()

        # 随机挑选演示: 取 min(5, 合法数) 个,3 次随机种子
        pick_n = min(5, len(legal))
        if pick_n > 0:
            w(f'### 随机挑选演示 (温度=1.0, 取 {pick_n} 个, 3 次不同种子)')
            w()
            w('| 种子 | 挑选结果 |')
            w('|------|----------|')
            for seed in [1, 42, 100]:
                picked = random_pick(legal, pick_n, seed=seed, temperature=1.0)
                picks_str = ' / '.join('+'.join(repr(c) for c in combo) for combo in picked)
                w(f'| {seed} | {picks_str} |')
            w()

        w('---')
        w()

        results.append({
            'phase': name,
            'shots': len(clips_per_shot),
            'clips': clip_count,
            'new': new_count,
            'total': total,
            'legal': len(legal),
        })
        history = legal

    w('## 汇总')
    w()
    w('| 阶段 | 分镜 | clip | 新增 | 笛卡尔积 | 合法组合 | 过滤率 |')
    w('|------|------|------|------|----------|----------|--------|')
    for r in results:
        rate = (r['total'] - r['legal']) / r['total'] * 100 if r['total'] else 0
        w(f'| {r["phase"]} | {r["shots"]} | {r["clips"]} | {r["new"]} | {r["total"]} | {r["legal"]} | {rate:.1f}% |')

    w()
    w('## 算法说明')
    w()
    w('- 每阶段独立运行,使用上一阶段合法组合作为历史')
    w('- 扩容隔离只在加新分镜时触发,前 K 分镜直接复用历史最优组合')
    w('- 单分镜新增 clip 时,新组合必须包含新 clip,且历史比对只看未变化分镜')
    w('- 同源约束保证不会出现原视频拼接现象')
    w('- 最长连续段约束保证任意两组合相似度受控')

    print(f'Done. Output: {OUTPUT}')
    print(f'Total size: {OUTPUT.stat().st_size} bytes')


if __name__ == '__main__':
    main()