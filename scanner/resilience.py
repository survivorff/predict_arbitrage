"""数据接入健壮性：限流、退避重试、熔断（Phase Two · 切片 C）。

真实平台 API 有速率限制，瞬时故障也时有发生。本模块把「韧性」做成与具体适配器
解耦的横切能力，通过 :class:`ResilientAdapter` 包装任意
:class:`~scanner.adapters.base.PlatformAdapter`，在其 ``fetch_markets`` /
``refresh_prices`` 外层叠加三层保护：

1. :class:`RateLimiter` —— 令牌桶限流，保证两次请求间隔不小于配置值，遵守平台速率限制。
2. :class:`Retry` —— 对 ``AdapterError`` 做有上限的指数退避重试，吸收瞬时故障。
3. :class:`CircuitBreaker` —— 连续失败达阈值则「熔断」，在冷却期内快速失败，避免对已
   不可用的平台持续打请求（雪崩）；冷却后进入「半开」，放行一次探测，成功则恢复（闭合），
   失败则重新熔断。

设计取向：
- 核心 :class:`~scanner.ingestion.IngestionService` 与具体适配器**零改动**——韧性是包装层。
- 所有组件注入 ``clock`` 与 ``sleep``，使限流间隔、退避时长、熔断冷却在测试中完全确定。
- 熔断打开时抛出 ``AdapterError``，沿用既有故障隔离语义：IngestionService 记录日志、
  保留上一份正常数据、继续其他适配器。

可观测性：``ResilientAdapter`` 暴露 :class:`AdapterMetrics`（成功/失败/限流次数/熔断状态等），
供 ``/health`` 端点汇报。

正确性属性（本切片新增）：
- Property 15（限流间隔）：相邻两次实际平台调用的时间间隔 ≥ ``min_interval``。
- Property 16（退避上限）：单次操作对底层适配器的调用次数 ≤ ``max_attempts``。
- Property 17（熔断状态机）：熔断在连续失败达阈值后打开；冷却期内调用快速失败、不触达底层；
  冷却后半开放行一次探测，成功则闭合、失败则重新打开。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable, List, Optional

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.models import CanonicalMarket

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - 薄封装
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# 限流：令牌桶 / 最小请求间隔
# ---------------------------------------------------------------------------


@dataclass
class RateLimiter:
    """基于最小请求间隔的限流器（Property 15）。

    保证相邻两次 :meth:`acquire` 之间至少间隔 ``min_interval`` 秒；不足则
    ``await sleep`` 补足。``clock`` 与 ``sleep`` 注入以保证确定性。
    """

    min_interval: float
    clock: Clock = _utc_now
    sleep: Sleep = _default_sleep
    _last_at: Optional[datetime] = field(default=None, init=False)

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        now = self.clock()
        if self._last_at is not None:
            elapsed = (now - self._last_at).total_seconds()
            wait = self.min_interval - elapsed
            if wait > 0:
                await self.sleep(wait)
                now = self.clock()
        self._last_at = now


# ---------------------------------------------------------------------------
# 退避重试
# ---------------------------------------------------------------------------


@dataclass
class Retry:
    """对 ``AdapterError`` 做有上限的指数退避重试（Property 16）。

    ``max_attempts`` 含首次尝试；即对底层的调用次数上限。第 n 次失败后等待
    ``backoff_base * 2**(n-1)`` 秒再重试。最后一次仍失败则抛出原始 ``AdapterError``。
    """

    max_attempts: int = 3
    backoff_base: float = 0.5
    sleep: Sleep = _default_sleep

    async def run(self, operation: Callable[[], Awaitable], *, name: str):
        attempts = max(1, self.max_attempts)
        last_exc: Optional[AdapterError] = None
        for attempt in range(1, attempts + 1):
            try:
                return await operation()
            except AdapterError as exc:
                last_exc = exc
                if attempt < attempts:
                    logger.warning(
                        "适配器 %s 第 %d/%d 次尝试失败：%s；将退避重试。",
                        name, attempt, attempts, exc,
                    )
                    await self.sleep(self.backoff_base * (2 ** (attempt - 1)))
                else:
                    logger.warning(
                        "适配器 %s 重试 %d 次后仍失败：%s。", name, attempts, exc,
                    )
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------


class CircuitState(str, Enum):
    """熔断器状态。"""

    CLOSED = "closed"      # 正常放行
    OPEN = "open"          # 熔断，冷却期内快速失败
    HALF_OPEN = "half_open"  # 冷却结束，放行一次探测


@dataclass
class CircuitBreaker:
    """连续失败熔断、冷却后半开探测恢复（Property 17）。

    - CLOSED：正常放行。连续失败计数达 ``failure_threshold`` → 转 OPEN。
    - OPEN：在 ``reset_timeout`` 冷却期内 :meth:`allow` 返回 False（调用方快速失败，
      不触达底层）；冷却结束后转 HALF_OPEN。
    - HALF_OPEN：放行一次探测；``record_success`` → CLOSED 并清零，``record_failure``
      → 重新 OPEN 并重置冷却计时。
    """

    failure_threshold: int = 5
    reset_timeout: float = 30.0
    clock: Clock = _utc_now
    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: Optional[datetime] = field(default=None, init=False)

    def allow(self) -> bool:
        """本次调用是否放行触达底层。"""
        if self.state is CircuitState.OPEN:
            # 冷却结束则转半开，放行一次探测。
            if self._opened_at is not None:
                elapsed = (self.clock() - self._opened_at).total_seconds()
                if elapsed >= self.reset_timeout:
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False
        # CLOSED 或 HALF_OPEN 均放行。
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        if self.state is CircuitState.HALF_OPEN:
            # 探测失败 → 重新熔断并重置冷却计时。
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self.state = CircuitState.OPEN
        self._opened_at = self.clock()


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


@dataclass
class AdapterMetrics:
    """单个适配器的接入指标，供 /health 汇报。"""

    name: str
    success_count: int = 0
    failure_count: int = 0
    rate_limited_count: int = 0
    circuit_rejected_count: int = 0
    circuit_state: str = CircuitState.CLOSED.value
    last_error: Optional[str] = None
    last_success_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 韧性包装器
# ---------------------------------------------------------------------------


class ResilientAdapter:
    """用限流 + 退避重试 + 熔断包装任意 :class:`PlatformAdapter`（切片 C）。

    对外仍是一个 ``PlatformAdapter``（``name`` / ``fetch_markets`` /
    ``refresh_prices``），因此 IngestionService 与具体适配器都无需改动。熔断打开时
    抛出 ``AdapterError``，沿用既有故障隔离语义。
    """

    def __init__(
        self,
        adapter: PlatformAdapter,
        *,
        min_interval: float = 0.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Optional[Clock] = None,
        sleep: Optional[Sleep] = None,
    ) -> None:
        self._adapter = adapter
        self.name = adapter.name
        clock = clock or _utc_now
        sleep = sleep or _default_sleep
        self._rate_limiter = RateLimiter(min_interval=min_interval, clock=clock, sleep=sleep)
        self._retry = Retry(max_attempts=max_attempts, backoff_base=backoff_base, sleep=sleep)
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
            clock=clock,
        )
        self._clock = clock
        self.metrics = AdapterMetrics(name=adapter.name)

    async def fetch_markets(self) -> List[CanonicalMarket]:
        return await self._guard(
            lambda: self._adapter.fetch_markets(), op_name="fetch_markets"
        )

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        return await self._guard(
            lambda: self._adapter.refresh_prices(markets), op_name="refresh_prices"
        )

    async def _guard(self, operation: Callable[[], Awaitable], *, op_name: str):
        # 1) 熔断检查：打开且未冷却则快速失败，不触达底层。
        if not self._breaker.allow():
            self.metrics.circuit_rejected_count += 1
            self.metrics.circuit_state = self._breaker.state.value
            raise AdapterError(
                f"熔断器打开，跳过 {self.name}.{op_name}", adapter=self.name
            )

        async def _attempt():
            # 2) 限流：每次实际调用前补足最小间隔。
            await self._rate_limiter.acquire()
            self.metrics.rate_limited_count += 1  # 统计经过限流闸门的调用次数
            return await operation()

        # 3) 退避重试包裹底层调用。
        try:
            result = await self._retry.run(_attempt, name=self.name)
        except AdapterError as exc:
            self._breaker.record_failure()
            self.metrics.failure_count += 1
            self.metrics.last_error = str(exc)
            self.metrics.circuit_state = self._breaker.state.value
            raise
        else:
            self._breaker.record_success()
            self.metrics.success_count += 1
            self.metrics.last_success_at = self._clock()
            self.metrics.circuit_state = self._breaker.state.value
            return result


__all__ = [
    "RateLimiter",
    "Retry",
    "CircuitBreaker",
    "CircuitState",
    "AdapterMetrics",
    "ResilientAdapter",
]
