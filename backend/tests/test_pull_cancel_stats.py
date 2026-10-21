# -*- coding: utf-8 -*-
"""拉取任务「取消时统计归零」回归测试。

# 线上症状（2026-10-21）：
  video_pull_log 连着几条 new_count=0 / skip_count=0 / fail_reason=NULL，
  而日志里明明有 `[拉取] 已过滤一条视频`，任务状态还是 success。

# 根因：
  `_TaskCancelled`（task_service）继承 Exception，被 _run_pull_round 的
  `except Exception` 接住 → 阶段 B 返回值的解包（`b_new, b_skip, ... = _run_detail_phase(...)`）
  **永不执行** → new_count / skip_count / detail_processed 全部停在初始值 0，
  且 `fail_reason = str(_TaskCancelled())` = "" → 写库成 NULL。
  状态也不是 cancelled（task_service 的 except _TaskCancelled 分支没机会跑），
  最终表现成"什么都没干但显示成功"。

# 本测试锁两件事：
  1. 取消信号必须穿透 _run_pull_round 的宽泛 except（不被当成普通异常吞掉）
  2. 取消发生在处理第 N 条时，task_service 应能正确置 cancelled
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import material_service as ms
from app.services.task_service import _TaskCancelled


def _make_client():
    client = MagicMock()
    client._fetch_aweme_detail = lambda vid, cookie="", account_id="", page=None: {
        "video_id": vid, "title": f"t{vid}", "_source": "detail",
    }
    return client


def _run_detail_3_items(cancel_after=None, db=None):
    """跑 3 条候选的阶段 B，cancel_after=N 表示第 N 条处理完时置取消信号。

    返回 (info, 处理条数)
    """
    info = MagicMock()
    info.start_ts = None
    info.progress = ""
    info.message = ""
    info.cancel_requested = False
    state = {"n": 0}

    def fake_process(video, category_id, client, **kw):
        state["n"] += 1
        if cancel_after is not None and state["n"] >= cancel_after:
            info.cancel_requested = True
        return {"action": "filtered", "material_id": None,
                "title": "t", "reason": "字幕"}

    db = db or MagicMock()
    db.query_one.return_value = None
    with patch.object(ms, "get_db", lambda: db), \
            patch.object(ms, "_process_single_video", fake_process):
        ms._run_detail_phase(
            "task1", ["1", "2", "3"], {}, _make_client(), "", "acc", 500, info,
            category_id="-", task_name="T", page=None,
        )
    return info, state["n"]


# ============ 取消必须穿透，不能被 except Exception 吞掉 ============

def test_cancel_signal_raises_task_cancelled_not_swallowed():
    """阶段 B 中途收到取消信号 → 抛 _TaskCancelled（供上层置 cancelled）。

    修复前：raise_for_cancel 抛的 _TaskCancelled 被 _run_pull_round 的
    `except Exception` 接住，统计全部归零且状态误判为 success。
    """
    with pytest.raises(_TaskCancelled):
        _run_detail_3_items(cancel_after=2)


def test_task_cancelled_is_exception_subclass():
    """_TaskCancelled 继承 Exception —— 这正是它会被宽泛 except 吞掉的原因。

    这条是"为什么会坏"的说明性测试：如果哪天它改成不继承 Exception，
    修复就不再必要，但也不该因此放松警惕（改成 BaseException 会更稳）。
    """
    assert issubclass(_TaskCancelled, Exception)


def test_cancel_exception_str_is_empty():
    """str(_TaskCancelled()) == "" —— 所以 fail_reason=str(e) 写库成 NULL。"""
    assert str(_TaskCancelled()) == ""


# ============ 统计在正常路径下确实正确（对照组）============

def test_detail_phase_returns_stats_without_cancel():
    """无取消时返回值必须正确（对照组，防止上面的修复误伤正常路径）。"""
    info = MagicMock()
    info.start_ts = None
    info.progress = ""
    info.message = ""
    info.cancel_requested = False

    def fake_process(video, category_id, client, **kw):
        return {"action": "filtered", "material_id": None,
                "title": "t", "reason": "字幕"}

    db = MagicMock()
    db.query_one.return_value = None
    with patch.object(ms, "get_db", lambda: db), \
            patch.object(ms, "_process_single_video", fake_process):
        res = ms._run_detail_phase(
            "task1", ["1", "2", "3"], {}, _make_client(), "", "acc", 500, info,
            category_id="-", task_name="T", page=None,
        )
    assert len(res) == 5
    b_new, b_skip, b_limit, b_reason, b_processed = res
    assert (b_new, b_skip, b_processed) == (0, 3, 3)
    assert b_limit is False
    assert not b_reason


def test_detail_phase_counts_new_and_duplicate():
    """new / duplicate / filtered 三类混合时的计数（对照组的细分）。"""
    info = MagicMock()
    info.start_ts = None
    info.progress = ""
    info.message = ""
    info.cancel_requested = False

    actions = {
        "1": {"action": "new", "material_id": "m1", "title": "a", "reason": ""},
        "2": {"action": "duplicate", "material_id": "m2", "title": "b",
              "reason": "已存在"},
        "3": {"action": "filtered", "material_id": None, "title": "c",
              "reason": "字幕"},
    }

    def fake_process(video, category_id, client, **kw):
        return actions[video["video_id"]]

    db = MagicMock()
    db.query_one.return_value = None
    with patch.object(ms, "get_db", lambda: db), \
            patch.object(ms, "_process_single_video", fake_process):
        res = ms._run_detail_phase(
            "task1", ["1", "2", "3"], {}, _make_client(), "", "acc", 500, info,
            category_id="-", task_name="T", page=None,
        )
    b_new, b_skip, _, _, b_processed = res
    assert b_new == 1
    assert b_skip == 2          # duplicate + filtered
    assert b_processed == 3


# ============ 源码级回归锁：防止 _TaskCancelled 的 except 分支被删掉 ============

def test_run_pull_round_has_task_cancelled_passthrough():
    """_run_pull_round 必须有 `except _TaskCancelled: ... raise`。

    故意锁源码而不是行为——行为测试（上面的 test_cancel_signal_raises）
    测的是 _run_detail_phase，而真正被吞的是 _run_pull_round 的 except 链。
    这里用 AST 检查，避免 `inspect.getsource` 匹配源码字符串的反模式
    （重构即假失败），只断言"存在一个 except handler 捕获 _TaskCancelled
    且里面有裸 raise"。
    """
    import ast
    src = (ROOT / "app" / "services" / "material_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def handler_catches(try_node, exc_name):
        for h in try_node.handlers:
            t = h.type
            if t is None:
                continue
            names = ([t.id] if isinstance(t, ast.Name)
                     else [e.id for e in t.elts if isinstance(e, ast.Name)])
            if exc_name in names:
                return h
        return None

    def find_try_in(fn):
        """函数体内（含嵌套）所有 Try 节点。"""
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Try):
                out.append(n)
        return out

    target = None
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_run_pull_round":
            target = fn
            break
    assert target is not None, "找不到 _run_pull_round"

    found = None
    for tnode in find_try_in(target):
        h = handler_catches(tnode, "_TaskCancelled")
        if h is not None:
            found = h
            break
    assert found is not None, \
        "_run_pull_round 缺少 except _TaskCancelled（取消异常会被 except Exception 吞掉）"
    assert any(isinstance(n, ast.Raise) and n.exc is None
               for n in ast.walk(found)), \
        "except _TaskCancelled 里必须有裸 raise 让异常穿透到 task_service"