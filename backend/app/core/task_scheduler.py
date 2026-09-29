# -*- coding: utf-8 -*-
"""APScheduler 定时调度封装（需求文档 3.3-1 任务模型）。

承载三类定时任务：
- 选品拉取任务 / 视频拉取任务（用户创建，interval_config 必填）；
- 账号登录态检测（系统内置，周期可配）；
- 数据日快照同步（系统内置，时刻列表可配）。

设计：
- BackgroundScheduler 单例，任务回调内部将实际工作投递到 task_service 队列（不在调度线程直接跑长任务）；
- 支持 interval 配置解析（每 X 分钟 / 每 X 小时 / 每天固定时刻）；
- 启动恢复：register_enabled_tasks() 读库重注册所有 enabled 任务。
"""

import json
import threading
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db import get_db
from app.db.utils import now_str

# APScheduler 实例（main.py 启动时 start_scheduler() 创建）
_scheduler: BackgroundScheduler | None = None

# 任务表刷新保护锁（避免调度线程与 API 线程同时改任务表）
_task_lock = threading.Lock()


def parse_interval_config(config_json: str) -> dict:
    """解析 interval_config JSON，校验必填字段。

    参数:
        config_json: {"type": "minute|hour|daily", "value": 30, "time": "09:00"}
    返回:
        解析后的 dict
    异常:
        ValueError 配置非法时抛出
    """
    cfg = json.loads(config_json)
    t = cfg.get("type")
    if t not in ("minute", "hour", "daily"):
        raise ValueError(f"interval_config.type 非法：{t}")
    if t in ("minute", "hour"):
        value = int(cfg.get("value", 0))
        if value <= 0:
            raise ValueError("interval_config.value 必须为正整数")
        if t == "minute" and value < 10:
            raise ValueError("最小间隔为 10 分钟（防风控）")
        cfg["value"] = value
    else:
        time_str = cfg.get("time", "09:00")
        parts = time_str.split(":")
        if len(parts) != 2 or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise ValueError(f"每日时刻非法：{time_str}")
    return cfg


def compute_next_run_time(config_json: str, base: datetime | None = None) -> str:
    """按 interval_config 计算下次执行时间（任务表 next_run_time 字段展示用）。

    参数:
        config_json: 间隔配置 JSON
        base: 计算基准时间（默认当前时间）
    返回:
        yyyy-MM-dd HH:mm:ss 字符串
    """
    cfg = parse_interval_config(config_json)
    base = base or datetime.now()
    if cfg["type"] == "minute":
        nxt = base + timedelta(minutes=cfg["value"])
    elif cfg["type"] == "hour":
        nxt = base + timedelta(hours=cfg["value"])
    else:
        hh, mm = cfg["time"].split(":")
        nxt = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if nxt <= base:
            nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


def _build_trigger(config_json: str):
    """按配置构造 APScheduler Trigger。"""
    cfg = parse_interval_config(config_json)
    if cfg["type"] == "minute":
        return IntervalTrigger(minutes=cfg["value"])
    if cfg["type"] == "hour":
        return IntervalTrigger(hours=cfg["value"])
    hh, mm = cfg["time"].split(":")
    return CronTrigger(hour=int(hh), minute=int(mm))


def start_scheduler() -> BackgroundScheduler:
    """启动调度器（幂等，重复调用返回既有实例）。"""
    global _scheduler
    if _scheduler is None:
        # APScheduler 内部线程默认 daemon=False，会阻塞解释器退出。
        # 提前 monkey-patch threading.Thread 让它创建 daemon 线程。
        import threading as _threading
        _orig = _threading.Thread.__init__

        def _daemon_init(self, *args, **kwargs):
            _orig(self, *args, **kwargs)
            self.daemon = True

        _threading.Thread.__init__ = _daemon_init
        _scheduler = BackgroundScheduler()
        _scheduler.start()
    return _scheduler


def register_interval_job(job_id: str, config_json: str, func: Callable, replace: bool = True) -> None:
    """注册（或替换）一个定时任务。

    参数:
        job_id: 任务唯一标识（如 shop_pull:{task_id}、account_check）
        config_json: interval_config JSON
        func: 回调（内部应投递到 task_service，不直接做长活）
        replace: 已存在时是否替换
    异常:
        ValueError 间隔配置非法时抛出
    """
    s = start_scheduler()
    trigger = _build_trigger(config_json)
    s.add_job(func, trigger=trigger, id=job_id, replace_existing=replace, max_instances=1, coalesce=True)


def remove_job(job_id: str) -> None:
    """移除定时任务（不存在时静默）。"""
    if _scheduler is not None:
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass


def shutdown_scheduler(wait: bool = False) -> None:
    """关闭调度器。

    参数:
        wait: 是否等所有正在执行的任务结束（默认 False，避免长任务阻塞进程退出；
              daemon 线程会被 _patch_uvicorn_force_exit 的 1s watchdog 兜底强退）
    """
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=wait)
        except Exception:  # noqa: BLE001 关闭异常不影响退出
            pass
        _scheduler = None


def register_enabled_tasks(register_shop_pull: Callable[[dict], None],
                           register_video_pull: Callable[[dict], None]) -> int:
    """启动时读库重注册所有 enabled 的定时拉取任务，返回注册数量。

    参数:
        register_shop_pull: 注册选品拉取的回调（参数为任务记录 dict）
        register_video_pull: 注册视频拉取的回调（参数为任务记录 dict）
    """
    d = get_db()
    count = 0
    for row in d.query_all("SELECT * FROM shop_pull_task WHERE status='enabled' AND deleted=0"):
        register_shop_pull(row)
        count += 1
    for row in d.query_all("SELECT * FROM video_pull_task WHERE status='enabled' AND deleted=0"):
        register_video_pull(row)
        count += 1
    # 刷新 next_run_time 展示字段
    for table in ("shop_pull_task", "video_pull_task"):
        for row in d.query_all(f"SELECT id, interval_config FROM {table} WHERE status='enabled' AND deleted=0"):
            d.update_by_id(table, row["id"], {"next_run_time": compute_next_run_time(row["interval_config"])})
    return count


def touch_last_run(table: str, task_id: str) -> None:
    """任务触发时刷新 last_run_time / next_run_time。"""
    d = get_db()
    row = d.query_one(f"SELECT interval_config FROM {table} WHERE id=?", (task_id,))
    if row:
        with _task_lock:
            d.update_by_id(table, task_id, {
                "last_run_time": now_str(),
                "next_run_time": compute_next_run_time(row["interval_config"]),
            })
