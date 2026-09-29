# -*- coding: utf-8 -*-
"""视频简介 + 项目-门店 绑定服务（#413 发布管理重设计）。

两张表：
- video_intro：项目下的视频简介（#415 存储合并：content 含 #话题，topics_json 字段保留但停用）
- project_shop：项目 ↔ 门店 多对多，sort_order 即发布顺序
"""
from __future__ import annotations

import re
import uuid

from app.db import get_db
from app.db.utils import now_str


# ---------- 视频简介 ----------

# (注：去掉 #413 审查 #7 中的 _now 重复，统一用 app.db.utils.now_str)

# #415：话题合并到 content 后，从内容用正则提取 #xxx 段作为 topics 字段返回。
# 匹配规则：以 # 开头、不含空白 / 其它 # 的一串字符（含中文/数字/字母/下划线/连字符）。
_TOPIC_RE = re.compile(r"#[^\s#]+")


def _extract_topics_from_content(content: str) -> list[str]:
    """#415：从 content 提取 #话题 列表。"""
    if not content:
        return []
    return _TOPIC_RE.findall(content)


def list_intros(project_id: str) -> list[dict]:
    """返回项目下所有未删的简介（按 create_time 升序）。
    #415：topics 从 content 用正则提取，不再读 topics_json 字段。
    """
    d = get_db()
    rows = d.query_all(
        "SELECT * FROM video_intro WHERE project_id=? AND deleted=0 ORDER BY create_time",
        (project_id,))
    for r in rows:
        r["topics"] = _extract_topics_from_content(r.get("content") or "")
    return rows


def create_intro(project_id: str, content: str, topics: list[str] | None = None) -> dict:
    """新增简介。

    #415：存储合并 — 若传 topics，自动追加到 content 末尾（空格分隔），
    库内只存 content；topics_json 字段保留但写 '[]'（不读）。
    topics 参数保留兼容旧 API 调用方，可传 None。
    """
    if not project_id:
        raise ValueError("project_id 不能为空")
    base = (content or "").strip()
    extra = " ".join(t.strip() for t in (topics or []) if t and t.strip())
    full = (base + (" " + extra if extra else "")).strip()
    if not full:
        raise ValueError("简介内容不能为空")
    d = get_db()
    intro_id = uuid.uuid4().hex
    now = now_str()
    d.insert("video_intro", {
        "id": intro_id, "project_id": project_id,
        "content": full,
        "topics_json": "[]",
        "enabled": 1,
        "create_time": now, "update_time": now,
    })
    return {"id": intro_id, "project_id": project_id, "content": full,
            "topics": _extract_topics_from_content(full)}


def update_intro(intro_id: str, content: str | None = None,
                 topics: list[str] | None = None) -> int:
    """更新简介内容/话题。

    #415：content 与 topics 合并存。传 content 时替换正文；传 topics 时追加到现有 content 末尾。
    两者同时传：content 为新正文，topics 追加其后再写。
    """
    d = get_db()
    fields: dict = {}
    if content is not None or topics is not None:
        old = d.query_one("SELECT content FROM video_intro WHERE id=?", (intro_id,))
        if not old:
            return 0
        base = (content.strip() if content is not None else (old.get("content") or ""))
        extra = " ".join(t.strip() for t in (topics or []) if t and t.strip())
        full = (base + (" " + extra if extra else "")).strip()
        if not full:
            raise ValueError("简介内容不能为空")
        fields["content"] = full
        fields["topics_json"] = "[]"
    if not fields:
        return 0
    return d.update_by_id("video_intro", intro_id, fields)


def delete_intro(intro_id: str) -> int:
    """软删简介。"""
    return get_db().soft_delete_by_id("video_intro", intro_id)


def set_project_intros(project_id: str, items: list[str]) -> int:
    """#415：全量覆盖项目下的视频简介。

    事务内：软删该项目所有旧简介 → 按传入顺序重建。
    items: list[str] —— 每条就是一行字符串（content 已含 #话题，由前端富文本按行解析）。
    返回写入条数。

    并发安全：参见 set_project_shops 的注释（SQLite + RLock + WAL 已天然防 lost update）。
    """
    if not project_id:
        raise ValueError("project_id 不能为空")
    cleaned: list[str] = []
    for it in items:
        s = (it or "").strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        # 空数组语义 = 全删（#414 全删全加覆盖语义保持）
        d = get_db()
        now = now_str()
        with d.transaction():
            d.execute(
                "UPDATE video_intro SET deleted=1, update_time=? "
                "WHERE project_id=? AND deleted=0",
                (now, project_id))
        return 0
    d = get_db()
    now = now_str()
    with d.transaction():
        d.execute(
            "UPDATE video_intro SET deleted=1, update_time=? "
            "WHERE project_id=? AND deleted=0",
            (now, project_id))
        for s in cleaned:
            d.insert("video_intro", {
                "id": uuid.uuid4().hex,
                "project_id": project_id,
                "content": s,
                "topics_json": "[]",
                "enabled": 1,
                "create_time": now, "update_time": now,
            })
    return len(cleaned)


# ---------- 项目-门店 绑定 ----------

def list_project_shops(project_id: str) -> list[dict]:
    """项目绑定的门店列表（按 sort_order 升序），含门店基础信息。

    #444：业务硬约束——只返回「未删 + 已加库 + 有佣金」的店；
    未加库 / 无佣金的店不能进入发布流程（与 #414 ProjectShopsDrawer UI 过滤对齐）。
    """
    d = get_db()
    return d.query_all(
        """SELECT ps.project_id, ps.shop_id, ps.sort_order,
                  s.name AS shop_name, s.city, s.category, s.poi_id
           FROM project_shop ps
           LEFT JOIN shop s ON s.id=ps.shop_id AND s.deleted=0
           WHERE ps.project_id=?
             AND s.id IS NOT NULL
             AND s.is_added_to_library=1
             AND s.is_cps=1
           ORDER BY ps.sort_order""",
        (project_id,))


def set_project_shops(project_id: str, shop_ids: list[str]) -> int:
    """全量覆盖：删除旧绑定，按传入顺序写入新绑定（sort_order 即勾选顺序）。

    并发安全说明：
    - 单进程：app.db.database 的 threading.RLock 串行化所有写，不存在 race。
    - 多进程：SQLite WAL + 单写者锁（默认 busy_timeout 等待），后到的事务会
      等到先到的事务 commit 后才执行 DELETE，看到完整状态后写入自己的数据。
    - 极端场景：若两进程同时到达，A 事务 DELETE 后 B 事务 BEGIN 前，A 还没
      commit，B 看到的仍是旧数据——B 的 DELETE 是幂等的，最终结果正确。
    """
    if not project_id:
        raise ValueError("project_id 不能为空")
    d = get_db()
    with d.transaction():
        d.execute("DELETE FROM project_shop WHERE project_id=?", (project_id,))
        for idx, sid in enumerate(shop_ids):
            d.execute(
                "INSERT INTO project_shop(project_id, shop_id, sort_order) VALUES (?, ?, ?)",
                (project_id, sid, idx))
    return len(shop_ids)


def list_projects_with_shops() -> list[dict]:
    """项目列表 + 每个项目的已绑定门店数（前端向导用）。"""
    d = get_db()
    return d.query_all(
        """SELECT p.id, p.title,
                  (SELECT COUNT(*) FROM project_shop ps WHERE ps.project_id=p.id) AS shop_count
           FROM project p WHERE p.deleted=0 ORDER BY p.create_time DESC""")


# #416：项目停用状态枚举（写入 project.status）
_VALID_PROJECT_STATUSES = ("normal", "disabled")


def set_project_status(project_id: str, status: str) -> int:
    """#416：更新项目状态（normal=启用 / disabled=停用）。
    停用后，新建发布任务（向导）不能再选该项目；停用不影响已有任务/成品/分镜。
    返回受影响行数；project_id 不存在返回 0。"""
    if not project_id:
        raise ValueError("project_id 不能为空")
    if status not in _VALID_PROJECT_STATUSES:
        raise ValueError(f"status 非法，应为 {_VALID_PROJECT_STATUSES}")
    d = get_db()
    n = d.execute(
        "UPDATE project SET status=?, update_time=? WHERE id=? AND deleted=0",
        (status, now_str(), project_id),
    )
    return n


def list_publishable_projects() -> list[dict]:
    """#416：只返回 status='normal' 的项目列表（用于新建发布任务时筛选可发布的项目）。
    返回字段同 list_projects_with_shops + status + 可用成品数（前端 step2 显示用）。"""
    d = get_db()
    return d.query_all(
        """SELECT p.id, p.title, p.status,
                  (SELECT COUNT(*) FROM project_shop ps WHERE ps.project_id=p.id) AS shop_count,
                  (SELECT COUNT(*) FROM generated_video gv WHERE gv.project_id=p.id AND gv.status='idle') AS generated_idle
           FROM project p WHERE p.deleted=0 AND p.status='normal' ORDER BY p.create_time DESC""")