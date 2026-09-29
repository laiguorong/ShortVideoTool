# -*- coding: utf-8 -*-
"""全局限速器（需求文档 NFR-12、F-02-R2）。

机制：
- 令牌桶控制全局请求速率（默认基线 2~5 秒随机间隔，可配）；
- 连续失败指数退避（1min/2min/4min）；
- 命中风控进入冷却（默认 2 小时），期间所有请求直接抛 RiskControlError。
"""

import random
import threading
import time

from app.core.douyin.base import RiskControlError


class RateLimiter:
    """全局请求限速器（模块级单例 rate_limiter）。"""

    def __init__(self, min_interval: float = 2.0, max_interval: float = 5.0):
        """初始化。

        参数:
            min_interval / max_interval: 相邻两次请求的随机间隔范围（秒）
        """
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._last_request_time: float = 0.0
        self._fail_streak = 0                 # 连续失败次数
        self._cooldown_until: float = 0.0     # 风控冷却截止时间戳
        self._lock = threading.Lock()

    # ---------- 请求前 ----------

    def acquire(self) -> None:
        """请求前调用：检查冷却 + 随机间隔等待。

        异常:
            RiskControlError 冷却期内直接抛出
        """
        with self._lock:
            now = time.time()
            # 风控冷却检查
            if now < self._cooldown_until:
                remain = int(self._cooldown_until - now)
                raise RiskControlError(f"风控冷却中，剩余 {remain} 秒")
            # 随机间隔等待（锁内 sleep 保证全局串行间隔）
            elapsed = now - self._last_request_time
            interval = random.uniform(self._min_interval, self._max_interval)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last_request_time = time.time()

    # ---------- 请求后 ----------

    def report_success(self) -> None:
        """请求成功：清零连续失败计数。"""
        with self._lock:
            self._fail_streak = 0

    def report_failure(self, retryable: bool = True) -> float:
        """请求失败：连续失败计数 + 指数退避建议。

        参数:
            retryable: 是否可重试失败（网络类 True；内容违规 False 不计退避）
        返回:
            建议退避秒数
        """
        with self._lock:
            if not retryable:
                return 0.0
            self._fail_streak += 1
            # 指数退避：60s → 120s → 240s
            return min(60 * (2 ** (self._fail_streak - 1)), 300)

    def reset(self) -> None:
        """手动解除冷却（用户确认后）。"""
        with self._lock:
            self._cooldown_until = 0.0
            self._fail_streak = 0


# 模块级单例
rate_limiter = RateLimiter()


def with_rate_limit():
    """装饰器工厂：为客户端方法自动加限速与失败统计。

    用法:
        @with_rate_limit()
        def search_videos(self, ...): ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            rate_limiter.acquire()
            try:
                result = func(*args, **kwargs)
                rate_limiter.report_success()
                return result
            except RiskControlError:
                raise  # 风控异常已由客户端设置冷却，不重复计失败
            except Exception:
                rate_limiter.report_failure()
                raise
        return wrapper
    return decorator
