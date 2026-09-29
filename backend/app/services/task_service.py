# -*- coding: utf-8 -*-
"""统一任务队列服务（参考 VideoMatrix task_service 模式）。

承载后台执行类任务：生成任务 / 发布明细执行 / 视频下载 / 手动数据同步。
定时任务（拉取/检测）由 task_scheduler 触发后回调 submit() 投入本队列执行。

设计：
- 模块级单例 task_service；
- 按任务类型分组 worker 池：generate（默认并发 1）、download（2）、publish（1）、pull（1）、sync（1）；
- 内存任务表 {task_id: TaskInfo} + threading.Lock；
- 状态机：waiting → running → success / partial / failed；running ⇄ paused（简化：取消即 cancelled）。
"""

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Optional

from loguru import logger

from app.db.utils import new_id, now_str
from app.services.setting_service import load_settings

# #P2-3：让所有新建线程默认 daemon=True（修复 Ctrl+C 无法退出）。
# 风险：会污染同进程内其他库（numpy/tornado 等）创建的线程。
# 当前实现稳定，标记为后续重构项（理想方案：自定义 _DaemonThread 子类 +
# 替换本服务 ThreadPoolExecutor 的 thread_factory，但 Python stdlib 无该参数）。
# 现阶段保留 monkey-patch，风险等级低（实测未引发问题）。
_orig_thread_init = threading.Thread.__init__


def _daemon_thread_init(self, *args, **kwargs):
    """monkey-patch：所有 threading.Thread 默认 daemon=True。"""
    _orig_thread_init(self, *args, **kwargs)
    self.daemon = True


threading.Thread.__init__ = _daemon_thread_init


# #P2-3 备选方案（暂未启用）：自定义 _DaemonThread 子类，供后续重构。
# class _DaemonThread(threading.Thread):
#     """默认 daemon=True 的 Thread 子类（仅供本服务线程池使用）。"""
#
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, daemon=True, **kwargs)

# 各任务类型的并发数（个人工具低并发，防风控/防卡顿）
# 任务队列类型（任务 #449 #407 重构后）：
# - video_pull：从原 pull 拆出的"素材拉取"（material_service 拉视频）
# - shop_pull：从原 pull 拆出的"门店拉取"（selection_service 拉 POI）
# - download 池 max_workers=1（与原 share_import_service 行为一致，防同账号撞 storage）
_CONCURRENCY: dict[str, int] = {
    "generate":   1,   # 视频生成（FFmpeg 重活，默认串行）
    "download":   1,   # 素材下载（视频/音乐分享链接导入，原 share_import 行为）
    "publish":    1,   # 发布执行（同账号严格串行）
    "video_pull": 1,   # 素材拉取
    "shop_pull":  1,   # 门店拉取
    "sync":       1,   # 数据同步
    "clip_cut":   1,   # 片段切割（#385：add-clips 异步队列；同类型串行，多文件并行由切割内部批量合并）
    "upload":     2,   # #119：本地文件上传（原 _EXECUTOR max_workers=2，IO + 抽帧）
}

# 任务类型中文名（前端展示）
_TYPE_LABELS: dict[str, str] = {
    "generate":   "视频生成",
    "download":   "素材下载",
    "publish":    "视频发布",
    "video_pull": "素材拉取",
    "shop_pull":  "门店拉取",
    "sync":       "数据同步",
    "clip_cut":   "片段切割",
    "upload":     "本地上传",
}


def _fmt_hms(seconds: float) -> str:
    """秒数 → 时:分:秒（如 1:23:45 / 0:05:30）。

    用于 func 体内 progress 字符串尾部"用时 X"统一显示。
    """
    s = max(0, int(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# worker 未设 message 时的默认中文文案（避免信息列显示英文 partial/failed）
_DEFAULT_MESSAGE: dict[str, str] = {
    "success": "",
    "partial": "部分成功",
    "failed": "失败",
    "cancelled": "已取消",
}


class TaskInfo:
    """单个后台任务的内存状态。"""

    def __init__(self, task_id: str, task_type: str, name: str, type_label: str | None = None):
        self.task_id = task_id
        self.task_type = task_type
        # type_label 可由 submit() 覆盖（如 download 区分视频/音乐 → "素材下载-视频"）
        self.type_label = type_label or _TYPE_LABELS.get(task_type, task_type)
        self.name = name                      # 展示名（如"生成任务 xxx"）
        self.status = "waiting"               # waiting/running/success/partial/failed/cancelled
        self.progress = ""                    # 进度描述（如"视频数 4/10，用时 0:05:30"）
        self.message = ""                     # 失败/异常原因（成功时留空）
        self.cancel_requested = False         # 取消请求标记（worker 内自查）
        self.create_time = now_str()
        self.start_time: Optional[str] = None  # 人类可读的开始时间（status=running 时填）
        self.end_time: Optional[str] = None
        # 任务 #66：worker 起跑时的 wall-clock 戳（time.time()）——所有 progress
        # "用时"按 time.time() - start_ts 算耗时，统一时钟避免混算。
        # 原用 monotonic() 但所有 consumer 写 time.time() - start_ts，时钟不一致
        # 导致 elapsed ≈ 56 年（当前 epoch - 系统启动时间），progress 显示 497098:31:37
        self.start_ts: Optional[float] = None

    def to_dict(self) -> dict:
        """转为 API 返回 dict。"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "type_label": self.type_label,
            "name": self.name,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "create_time": self.create_time,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }


class TaskService:
    """后台任务队列单例。"""

    def __init__(self):
        """初始化各类型线程池与任务表。"""
        self._pools: dict[str, ThreadPoolExecutor] = {
            t: ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"task-{t}")
            for t, n in _CONCURRENCY.items()
        }
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = threading.Lock()

    # ---------- 提交与执行 ----------

    def submit(self, task_type: str, name: str, func: Callable[[TaskInfo], Any],
               type_label: str | None = None) -> str:
        """提交后台任务，立即返回任务 ID（不阻塞）。

        参数:
            task_type: 任务类型（generate/download/publish/video_pull/shop_pull/sync/clip_cut）
            name: 展示名（业务表 task_name 直接传，不再拼接后缀）
            func: 任务函数，入参为 TaskInfo（worker 内可读 cancel_requested、更新 progress）
            type_label: 覆盖默认中文标签。仅 download 类使用（细分"素材下载-视频/音乐"）。
                其他 type 传 None 走 _TYPE_LABELS 默认。
        返回:
            任务 ID（UUID）
        """
        if task_type not in self._pools:
            raise ValueError(f"未知任务类型：{task_type}")
        info = TaskInfo(new_id(), task_type, name, type_label=type_label)
        with self._lock:
            self._tasks[info.task_id] = info
            # #高危-1：每次 submit 触发一次终态任务清理（无锁竞争时极低开销）
            self._cleanup_finished_locked()

        def _run():
            import time as _time
            info.status = "running"
            info.start_time = now_str()
            # 任务 #66：worker 起跑时间戳统一来源
            info.start_ts = _time.time()
            logger.info("[任务开始] {t} {name}（id={id}）", t=_TYPE_LABELS.get(task_type, task_type), name=name, id=info.task_id)
            try:
                result = func(info)
                # 任务 #59 P0 #1：状态机契约——func 可返回 "success"/"partial"/"failed"
                # 其余返回（None/字典/其他字符串）一律视为 success（兼容旧 worker）
                if result in ("success", "partial", "failed"):
                    info.status = result
                else:
                    info.status = "success"
                # 任务 #367：func 可能已设 info.message(失败原因/抓取详情),优先保留;
                # 未设置时按 status 默认填中文（避免信息列显示英文 partial/failed）
                if not info.message:
                    info.message = _DEFAULT_MESSAGE.get(info.status, str(result))
                logger.info("[任务完成] {name} → {status}{msg}", name=name, status=info.status,
                            msg=f"：{info.message}" if info.message else "")
            except _TaskCancelled:
                info.status = "cancelled"
                info.message = "已取消"
                logger.info("[任务取消] {name}（id={id}）", name=name, id=info.task_id)
            except Exception as e:  # noqa: BLE001 兜底捕获，任务失败不崩进程
                info.status = "failed"
                info.message = f"{e}"
                logger.error("[任务失败] {name}（id={id}）：{e}\n{tb}",
                             name=name, id=info.task_id, e=e, tb=traceback.format_exc())
            finally:
                info.end_time = now_str()

        # 任务 #59 P0 #11：_pools.submit 失败时清理 _tasks 避免僵尸记录
        try:
            self._pools[task_type].submit(_run)
        except RuntimeError as e:
            # 线程池已关闭（应用退出时），清掉占位 info
            with self._lock:
                self._tasks.pop(info.task_id, None)
            logger.warning("[任务] 提交失败（线程池关闭?）{name}（id={id}）：{e}",
                           name=name, id=info.task_id, e=e)
            raise
        return info.task_id

    # ---------- 查询与控制 ----------

    def get_task(self, task_id: str) -> Optional[dict]:
        """查询单个任务。"""
        with self._lock:
            info = self._tasks.get(task_id)
            # 任务 #59 P0 #12：锁内 to_dict，避免 worker 并发修改撕裂
            return info.to_dict() if info else None

    def list_tasks(self, running_only: bool = False) -> list[dict]:
        """任务列表（时间倒序）。"""
        # 任务 #59 P0 #12：锁内一次性构造快照（Lock 不可重入）
        with self._lock:
            items = list(self._tasks.values())
            if running_only:
                items = [i for i in items if i.status in ("waiting", "running")]
            items.sort(key=lambda i: i.create_time, reverse=True)
            return [i.to_dict() for i in items]

    def summary(self) -> dict:
        """任务概览（底部状态栏 + TasksPage 顶部 RunningTasksBanner 数据源）。

        返回 running_total / by_type / running_items（含 status/progress/message
        用于状态栏显示"任务运行中：{name} - {progress}"）。
        """
        # 任务 #59 P0 #12：锁内一次性构造快照（Lock 不可重入）
        with self._lock:
            items = list(self._tasks.values())
            running = [i for i in items if i.status in ("waiting", "running")]
            return {
                "running_total": len(running),
                "by_type": {t: len([i for i in running if i.task_type == t]) for t in _CONCURRENCY},
                "running_items": [
                    {
                        "task_id": i.task_id,
                        "type": i.task_type,
                        "type_label": i.type_label,
                        "name": i.name,
                        "status": i.status,
                        "progress": i.progress,
                        "message": i.message,
                    }
                    for i in running
                ],
            }

    def request_cancel(self, task_id: str) -> bool:
        """请求取消任务（worker 自查标记后停止），返回任务是否存在。"""
        with self._lock:
            info = self._tasks.get(task_id)
            if info and info.status in ("waiting", "running"):
                info.cancel_requested = True
                return True
            return False

    def _cleanup_finished_locked(self) -> None:
        """#高危-1：清理过期/超量终态任务，防止 _tasks dict 无限增长 → OOM。

        调用方必须已持有 self._lock。
        清理策略：
        1. 先按 task_history_retention_hours 淘汰过期终态任务
        2. 若仍超 task_history_max_count，按 end_time 升序淘汰最早的
        3. 仅清理 6 个终态：success/partial/failed/cancelled/waiting(异常遗留) + 无 status
        4. running 永不清理（worker 仍在执行）
        """
        try:
            settings = load_settings()
            max_count = int(settings.get("task_history_max_count", 500))
            retention_hours = int(settings.get("task_history_retention_hours", 24))
        except (OSError, ValueError, TypeError):
            # 配置读取异常 → 跳过清理（宁可内存涨也不误清）
            return

        finished_statuses = {"success", "partial", "failed", "cancelled"}
        # 1) 按时间淘汰
        if retention_hours > 0:
            cutoff = datetime.now().timestamp() - retention_hours * 3600
            for tid, info in list(self._tasks.items()):
                if info.status not in finished_statuses:
                    continue
                end = info.end_time or info.create_time
                try:
                    if end and datetime.strptime(end, "%Y-%m-%d %H:%M:%S").timestamp() < cutoff:
                        self._tasks.pop(tid, None)
                except (ValueError, TypeError):
                    pass
        # 2) 按数量淘汰（保留 max_count 条最新终态任务）
        if max_count > 0:
            finished = [(tid, i) for tid, i in self._tasks.items()
                        if i.status in finished_statuses]
            if len(finished) > max_count:
                finished.sort(key=lambda kv: kv[1].end_time or kv[1].create_time or "")
                excess = len(finished) - max_count
                for tid, _ in finished[:excess]:
                    self._tasks.pop(tid, None)

    def shutdown(self) -> None:
        """关闭所有线程池（应用退出时调用）。

        不阻塞等任务结束（wait=False）：worker 线程已 daemon 化，主进程退出时被回收；
        避免长任务（FFmpeg/Playwright）阻塞 Ctrl+C 退出路径。
        cancel_futures=True：让排队的任务直接取消，未启动的不再执行。
        """
        # #高危-1：shutdown 兜底清理（处理运行中→终态但已超期的任务）
        with self._lock:
            self._cleanup_finished_locked()
        for pool in self._pools.values():
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python < 3.9 不支持 cancel_futures，退化到非阻塞
                pool.shutdown(wait=False)


class _TaskCancelled(Exception):
    """任务被取消（worker 内 raise_for_cancel 抛出）。"""


def raise_for_cancel(info: TaskInfo) -> None:
    """worker 循环内调用：收到取消请求则抛异常终止。"""
    if info.cancel_requested:
        raise _TaskCancelled()


def interruptible_sleep(seconds: float, info: TaskInfo, chunk: float = 1.0) -> None:
    """分段 sleep + 取消信号检测。

    #P1-3：发布重试 time.sleep 阻塞时取消信号无法响应，
    用户点取消后最长要等 5 分钟才退出。改为按 chunk(默认 1s) 分段，
    每段检查 cancel_requested，响应速度从分钟级降到秒级。
    """
    import time as _t
    end = _t.monotonic() + max(0.0, seconds)
    while True:
        remaining = end - _t.monotonic()
        if remaining <= 0:
            return
        if info.cancel_requested:
            raise _TaskCancelled()
        _t.sleep(min(chunk, remaining))


# 模块级单例
task_service = TaskService()
