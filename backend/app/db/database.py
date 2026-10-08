# -*- coding: utf-8 -*-
"""SQLite 数据库连接管理。

设计（个人单机工具，从简）：
- 模块级单例 Database，单一 sqlite3 连接 + threading.RLock 串行化读写；
- 开启 WAL（Write-Ahead Logging，提升并发读写性能，见需求文档术语表）与外键约束；
- 提供 execute / query_one / query_all / query_page / insert / update 辅助方法，返回 dict；
- 启动时执行 migrations.py 中的建表迁移（PRAGMA user_version 驱动）。
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.db.utils import fill_common_fields

# data 目录根（backend 运行目录上一级的 data，见需求文档 F-08.3；实际由 setting_service 初始化后传入）
_DB_PATH: Optional[Path] = None


class Database:
    """SQLite 数据库封装（模块级单例 db）。"""

    def __init__(self, db_path: Path):
        """初始化连接并开启 WAL 与外键。

        参数:
            db_path: 数据库文件完整路径（data/db/short_video_tools.db）
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # 事务上下文标志（事务内的 execute 不自动 commit，由 transaction() 退出时统一处理）
        self._in_transaction = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")

    # ---------- 事务 ----------

    @contextmanager
    def transaction(self, isolation: str = "deferred"):
        """事务上下文：BEGIN → yield → COMMIT/ROLLBACK。

        isolation:
        - 'deferred'（默认）：延迟获取写锁，并发 SELECT 看到一致快照
        - 'immediate'：BEGIN 时立即抢写锁；并发场景第二个 BEGIN 阻塞等第一个 COMMIT，
          解决「两事务都查到相同 in-flight → 重复选择同一资源」问题
          （典型场景：_confirm_video_dir scheduler 选 vp 前查锁）

        异常时整体回滚；正常结束统一 commit。事务内调用的 execute()/insert()/update_by_id()
        不会触发自动 commit（由本方法末尾统一提交）。
        """
        with self._lock:
            if self._in_transaction:
                # 嵌套：复用外层事务（sqlite savepoint 不必，简单 BEGIN 即可，commit 仅最外层触发）
                yield
                return
            self._in_transaction = True
            begin_sql = "BEGIN" if isolation == "deferred" else f"BEGIN {isolation.upper()}"
            self._conn.execute(begin_sql)
            try:
                yield
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            else:
                self._conn.commit()
            finally:
                self._in_transaction = False

    # ---------- 基础执行 ----------

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """执行写语句（INSERT/UPDATE/DELETE），事务内不自动 commit，返回受影响行数。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            if not self._in_transaction:
                self._conn.commit()
            return cur.rowcount

    def executescript(self, sql: str) -> None:
        """执行多语句脚本（建表迁移用）。"""
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def query_one(self, sql: str, params: tuple | dict = ()) -> Optional[dict]:
        """查询单条记录，返回 dict 或 None。"""
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        """查询多条记录，返回 dict 列表。"""
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_page(self, sql: str, params: tuple | dict = (), page: int = 1, page_size: int = 20) -> dict:
        """分页查询。

        参数:
            sql: 查询 SQL（不含 LIMIT）
            params: SQL 参数
            page: 页码（1 起）
            page_size: 每页条数
        返回:
            {"total": 总数, "page": 页码, "page_size": 每页条数, "list": 当前页记录}
        """
        # 将原 SQL 包一层 count 求总数（ORDER BY 对 count 无影响但语法允许，直接包装）
        count_sql = f"SELECT COUNT(*) FROM ({sql})"
        total = self.query_one(count_sql, params)["COUNT(*)"]
        offset = (page - 1) * page_size
        page_sql = f"{sql} LIMIT {page_size} OFFSET {offset}"
        rows = self.query_all(page_sql, params)
        return {"total": total, "page": page, "page_size": page_size, "list": rows}

    # ---------- 便捷写入 ----------

    def insert(self, table: str, record: dict) -> str:
        """插入记录并自动补通用字段（仅补表内实际存在的列），返回主键 ID。

        纯流水表（日志/关联表等）可能没有 deleted 列，通用字段按表结构裁剪。
        """
        fill_common_fields(record, is_insert=True)
        # 查表实际列，裁剪不存在的通用字段
        with self._lock:
            cols_in_table = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for k in [k for k in ("id", "create_time", "update_time", "deleted") if k not in cols_in_table]:
            record.pop(k, None)
        cols = ", ".join(record.keys())
        marks = ", ".join(["?"] * len(record))
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(record.values()))
        return record.get("id", "")

    def update_by_id(self, table: str, record_id: str, fields: dict) -> int:
        """按主键更新记录并刷新 update_time（仅刷新表内存在的通用列），返回受影响行数。"""
        fill_common_fields(fields, is_insert=False)
        with self._lock:
            cols_in_table = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        # 裁剪表内不存在的通用列（如 project_shot_clip 无 update_time/deleted）
        for k in [k for k in ("update_time", "deleted") if k not in cols_in_table]:
            fields.pop(k, None)
        sets = ", ".join([f"{k}=?" for k in fields])
        params = tuple(fields.values()) + (record_id,)
        return self.execute(f"UPDATE {table} SET {sets} WHERE id=?", params)

    def soft_delete_by_id(self, table: str, record_id: str) -> int:
        """逻辑删除（deleted=1），返回受影响行数。"""
        return self.execute(
            f"UPDATE {table} SET deleted=1, update_time=? WHERE id=?",
            (fill_common_fields({}, is_insert=False)["update_time"], record_id),
        )

    def user_version(self) -> int:
        """读取数据库结构版本号。"""
        row = self.query_one("PRAGMA user_version")
        return row["user_version"]

    def set_user_version(self, version: int) -> None:
        """写入数据库结构版本号。"""
        self.execute(f"PRAGMA user_version={version}")

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            self._conn.close()


# 模块级单例：由 app.main 启动时调用 init_db() 创建
db: Optional[Database] = None


# 旧 db 文件名常量（#data-dir-choice / 去抖音化前的硬编码；保留以做自动迁移）
_LEGACY_DB_NAME = "douyin_tools.db"


def _migrate_legacy_db_filename(new_path: Path) -> None:
    """旧名 db 文件 → 新名自动迁移。

    场景：早期版本 db 硬编码 douyin_tools.db；去抖音化后改 short_video_tools.db。
    检测旧名（主文件 + WAL/SHM 三个）存在 → 一次性 rename 到新名（用户数据保留）。
    - 仅在旧存在 + 新不存在时迁移；新存在则跳过（防覆盖用户新数据）
    """
    if new_path.exists():
        return  # 新名已有 → 跳过（幂等）
    old_path = new_path.parent / _LEGACY_DB_NAME
    if not old_path.exists():
        return  # 旧名也不存在 → 首次启动场景
    # WAL/SHM 三个文件一起 rename
    for ext in ("", "-wal", "-shm"):
        src = old_path.with_name(old_path.name + ext)
        dst = new_path.with_name(new_path.name + ext)
        if src.exists():
            try:
                src.rename(dst)
            except OSError as e:
                # 迁移失败不阻塞（保留旧文件，下次启动再试）
                import sys
                print(f"[db migrate] {src.name} → {dst.name} 失败: {e}", file=sys.stderr)
                return


def init_db(db_path: Path) -> Database:
    """初始化全局数据库单例（含旧文件名迁移）。"""
    _migrate_legacy_db_filename(db_path)
    global db
    db = Database(db_path)
    return db


def get_db() -> Database:
    """获取全局数据库单例，未初始化则抛错。"""
    if db is None:
        raise RuntimeError("数据库未初始化，请先在应用启动时调用 init_db()")
    return db
