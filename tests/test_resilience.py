"""数据接入健壮性测试（Phase Two · 切片 C）。

覆盖 scanner.resilience 的四个组件：
- RateLimiter（Property 15 限流间隔）
- Retry（Property 16 退避上限）
- CircuitBreaker（Property 17 熔断状态机）
- ResilientAdapter（三层保护的集成行为）

所有时间相关行为通过可前进的 ``FakeClock`` 与记录用的 ``RecordingSleep`` 注入，
使限流间隔、退避时长、熔断冷却完全确定、离线可复现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.models import CanonicalMarket, Outcome
from scanner.resilience import (
    AdapterMetrics,
    CircuitBreaker,
    CircuitState,
    RateLimiter,
    ResilientAdapter,
    Retry,
)
from tests.adapter_contract import FakeAdapter

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 测试替身：可前进时钟、记录用 sleep、计数适配器
# --------------------------------------------------------------------------- #
class FakeClock:
    """确定性、可前进的 UTC 时钟（参考 tests/test_ingestion.py）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class RecordingSleep:
    """记录被请求的 sleep 时长的异步回调。

    简单起见仅记录时长、不真正等待、也不前进时钟；涉及限流间隔的测试会显式
    advance 注入的 FakeClock。
    """

    def __init__(self) -> None:
        self.calls: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class CountingAdapter:
    """记录底层调用次数的适配器；可注入失败（失败时仍计数）。

    FakeAdapter 在 ``fetch_error`` 注入时会在自增 ``fetch_calls`` 之前就抛出，
    因此失败路径无法用它统计「底层被调用了几次」。本适配器无论成功失败都计数，
    用于验证退避重试次数与「熔断后不触达底层」。
    """

    def __init__(
        self,
        *,
        name: str = "counting",
        markets: Optional[List[CanonicalMarket]] = None,
        fail: bool = False,
    ) -> None:
        self.name = name
        self.fetch_calls = 0
        self.refresh_calls = 0
        self.fail = fail
        self._markets = markets if markets is not None else [_market("counting", "m1")]

    async def fetch_markets(self) -> List[CanonicalMarket]:
        self.fetch_calls += 1
        if self.fail:
            raise AdapterError("boom", adapter=self.name)
        return list(self._markets)

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        self.refresh_calls += 1
        if self.fail:
            raise AdapterError("boom", adapter=self.name)
        return list(markets)


def _market(platform: str, market_id: str, *, price: float = 0.6) -> CanonicalMarket:
    """构造一个最小可用的规范市场。"""
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=f"{platform}:{market_id}",
        outcomes=[Outcome(name="YES", price=price), Outcome(name="NO", price=1 - price)],
        volume_usd=1000.0,
        liquidity_usd=500.0,
        fee_rate=0.0,
        retrieved_at=BASE_TIME,
    )


# =========================================================================== #
# RateLimiter（Property 15：相邻两次 acquire 间隔 >= min_interval）
# =========================================================================== #
async def test_rate_limiter_first_acquire_does_not_wait():
    # 首次 acquire 没有「上一次」，不应等待。
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = RateLimiter(min_interval=1.0, clock=clock, sleep=sleep)

    await limiter.acquire()

    assert sleep.calls == []


async def test_rate_limiter_second_acquire_waits_when_clock_not_advanced():
    # 紧接着第二次 acquire（时钟未前进）应等待约 min_interval。
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = RateLimiter(min_interval=1.0, clock=clock, sleep=sleep)

    await limiter.acquire()
    await limiter.acquire()

    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(1.0)


async def test_rate_limiter_does_not_wait_when_enough_time_elapsed():
    # 两次之间时钟已前进超过 min_interval，第二次不应等待。
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = RateLimiter(min_interval=1.0, clock=clock, sleep=sleep)

    await limiter.acquire()
    clock.advance(2.0)  # 超过最小间隔
    await limiter.acquire()

    assert sleep.calls == []


async def test_rate_limiter_partial_elapsed_waits_remaining():
    # 已过去一部分时间时，只补足剩余间隔。
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = RateLimiter(min_interval=1.0, clock=clock, sleep=sleep)

    await limiter.acquire()
    clock.advance(0.3)
    await limiter.acquire()

    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(0.7)


async def test_rate_limiter_zero_interval_never_waits():
    # min_interval=0 时从不限流。
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = RateLimiter(min_interval=0.0, clock=clock, sleep=sleep)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert sleep.calls == []


# =========================================================================== #
# Retry（Property 16：底层调用次数 <= max_attempts）
# =========================================================================== #
async def test_retry_exhausts_attempts_then_raises():
    # operation 连续抛 AdapterError：恰好调用 max_attempts 次后抛出。
    sleep = RecordingSleep()
    retry = Retry(max_attempts=3, backoff_base=0.5, sleep=sleep)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise AdapterError("boom")

    with pytest.raises(AdapterError):
        await retry.run(_op, name="x")

    assert calls["n"] == 3


async def test_retry_returns_on_success_without_further_attempts():
    # 第 k 次成功（k<max_attempts）：调用 k 次后返回，不再继续。
    sleep = RecordingSleep()
    retry = Retry(max_attempts=3, backoff_base=0.5, sleep=sleep)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        if calls["n"] < 2:
            raise AdapterError("boom")
        return "ok"

    result = await retry.run(_op, name="x")

    assert result == "ok"
    assert calls["n"] == 2


async def test_retry_backoff_sequence_is_exponential():
    # 退避时长序列为 backoff_base * 2**(n-1)：max_attempts=3,base=0.5 → [0.5, 1.0]。
    sleep = RecordingSleep()
    retry = Retry(max_attempts=3, backoff_base=0.5, sleep=sleep)

    async def _op():
        raise AdapterError("boom")

    with pytest.raises(AdapterError):
        await retry.run(_op, name="x")

    assert sleep.calls == [pytest.approx(0.5), pytest.approx(1.0)]


async def test_retry_does_not_retry_non_adapter_error():
    # 非 AdapterError（ValueError）不被重试，直接冒泡，只调用一次。
    sleep = RecordingSleep()
    retry = Retry(max_attempts=3, backoff_base=0.5, sleep=sleep)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await retry.run(_op, name="x")

    assert calls["n"] == 1
    assert sleep.calls == []


async def test_retry_raises_last_adapter_error():
    # 最后仍失败时抛出的是「最后一个」AdapterError。
    sleep = RecordingSleep()
    retry = Retry(max_attempts=2, backoff_base=0.5, sleep=sleep)
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise AdapterError(f"fail-{calls['n']}")

    with pytest.raises(AdapterError) as excinfo:
        await retry.run(_op, name="x")

    assert "fail-2" in str(excinfo.value)


# =========================================================================== #
# CircuitBreaker（Property 17：熔断状态机）
# =========================================================================== #
def test_breaker_opens_after_threshold_failures():
    # 连续失败达 failure_threshold 后 state==OPEN，allow() 返回 False。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=30.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True

    breaker.record_failure()  # 第三次达阈值
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


def test_breaker_open_within_cooldown_stays_closed_to_traffic():
    # OPEN 且未到 reset_timeout：allow() 仍 False。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)

    breaker.record_failure()  # 立即打开
    assert breaker.state is CircuitState.OPEN

    clock.advance(29.0)  # 未到冷却时长
    assert breaker.allow() is False
    assert breaker.state is CircuitState.OPEN


def test_breaker_transitions_to_half_open_after_cooldown():
    # OPEN 且时钟前进 >= reset_timeout：allow() 返回 True 且转 HALF_OPEN。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)

    breaker.record_failure()
    clock.advance(30.0)  # 恰好达到冷却时长

    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_breaker_half_open_success_closes_and_resets():
    # HALF_OPEN 下 record_success → CLOSED 且失败清零（allow True）。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=30.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()  # OPEN
    clock.advance(30.0)
    assert breaker.allow() is True  # → HALF_OPEN

    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True
    # 失败计数已清零：再来一次失败不应立即重新打开。
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


def test_breaker_half_open_failure_reopens_and_resets_timer():
    # HALF_OPEN 下 record_failure → 重新 OPEN（allow False，opened_at 重置为当前 clock）。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)

    breaker.record_failure()  # OPEN at t=0
    clock.advance(30.0)
    assert breaker.allow() is True  # → HALF_OPEN at t=30

    breaker.record_failure()  # 探测失败 → 重新熔断，opened_at 重置为 t=30
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False

    clock.advance(29.0)  # 距重置时刻仅 29s，仍在冷却内
    assert breaker.allow() is False
    clock.advance(1.0)  # 累计达 30s
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_breaker_closed_success_resets_failure_count():
    # CLOSED 下 record_success 清零失败计数（未达阈值的失败累计可被成功重置）。
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=30.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()  # 累计 2，未达阈值 3
    breaker.record_success()  # 清零

    breaker.record_failure()
    breaker.record_failure()  # 重新累计到 2，仍未达阈值
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True


# =========================================================================== #
# ResilientAdapter（集成）
# =========================================================================== #
async def test_resilient_adapter_is_a_platform_adapter():
    # 对外满足 PlatformAdapter Protocol（runtime_checkable）。
    inner = FakeAdapter(name="fake")
    wrapped = ResilientAdapter(inner, clock=FakeClock(), sleep=RecordingSleep())

    assert isinstance(wrapped, PlatformAdapter)
    assert wrapped.name == "fake"


async def test_resilient_adapter_success_path_records_metrics():
    # 成功路径：fetch_markets 返回数据，success_count==1，状态闭合，记录 last_success_at。
    clock = FakeClock()
    inner = FakeAdapter([_market("fake", "m1")], name="fake")
    wrapped = ResilientAdapter(inner, clock=clock, sleep=RecordingSleep())

    markets = await wrapped.fetch_markets()

    assert [m.market_id for m in markets] == ["m1"]
    assert isinstance(wrapped.metrics, AdapterMetrics)
    assert wrapped.metrics.success_count == 1
    assert wrapped.metrics.failure_count == 0
    assert wrapped.metrics.circuit_state == "closed"
    assert wrapped.metrics.last_success_at == BASE_TIME


async def test_resilient_adapter_rate_limits_consecutive_fetches():
    # 限流生效：连续两次 fetch（时钟不前进，min_interval>0）→ 第二次触发等待。
    clock = FakeClock()
    sleep = RecordingSleep()
    inner = FakeAdapter([_market("fake", "m1")], name="fake")
    wrapped = ResilientAdapter(inner, min_interval=1.0, clock=clock, sleep=sleep)

    await wrapped.fetch_markets()
    await wrapped.fetch_markets()  # 时钟未前进 → 需等待补足间隔

    assert len(sleep.calls) == 1
    assert sleep.calls[0] == pytest.approx(1.0)
    assert wrapped.metrics.success_count == 2


async def test_resilient_adapter_retries_with_backoff_then_fails():
    # 退避重试：底层始终失败且 max_attempts=2 → 调用底层 2 次后抛 AdapterError。
    clock = FakeClock()
    sleep = RecordingSleep()
    inner = CountingAdapter(name="counting", fail=True)
    wrapped = ResilientAdapter(
        inner, max_attempts=2, backoff_base=0.5, clock=clock, sleep=sleep
    )

    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()

    assert inner.fetch_calls == 2  # 底层被尝试 2 次
    assert wrapped.metrics.failure_count == 1  # 整次操作记一次失败
    assert sleep.calls == [pytest.approx(0.5)]  # 仅一次退避（第 1 次失败后）
    assert wrapped.metrics.last_error is not None


async def test_resilient_adapter_opens_circuit_and_fast_fails():
    # 熔断打开后快速失败：failure_threshold=2，两次失败后第三次不触达底层。
    clock = FakeClock()
    sleep = RecordingSleep()
    inner = CountingAdapter(name="counting", fail=True)
    wrapped = ResilientAdapter(
        inner,
        max_attempts=1,  # 不重试，便于精确计数
        failure_threshold=2,
        reset_timeout=30.0,
        clock=clock,
        sleep=sleep,
    )

    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()  # 失败 1
    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()  # 失败 2 → 熔断打开

    assert inner.fetch_calls == 2
    rejected_before = wrapped.metrics.circuit_rejected_count

    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()  # 熔断打开 → 快速失败

    assert inner.fetch_calls == 2  # 底层未被再次触达
    assert wrapped.metrics.circuit_rejected_count == rejected_before + 1
    assert wrapped.metrics.circuit_state == "open"


async def test_resilient_adapter_recovers_after_cooldown():
    # 熔断冷却后恢复：前进 >= reset_timeout 且底层恢复正常，再 fetch 成功，状态回闭合。
    clock = FakeClock()
    sleep = RecordingSleep()
    inner = CountingAdapter(name="counting", fail=True)
    wrapped = ResilientAdapter(
        inner,
        max_attempts=1,
        failure_threshold=2,
        reset_timeout=30.0,
        clock=clock,
        sleep=sleep,
    )

    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()
    with pytest.raises(AdapterError):
        await wrapped.fetch_markets()  # 熔断打开
    assert wrapped.metrics.circuit_state == "open"

    # 冷却 + 底层恢复
    clock.advance(30.0)
    inner.fail = False

    markets = await wrapped.fetch_markets()  # 半开探测成功 → 闭合

    assert len(markets) == 1
    assert wrapped.metrics.circuit_state == "closed"
    assert wrapped.metrics.success_count == 1


async def test_resilient_adapter_refresh_prices_happy_path():
    # refresh_prices 也走同样的 guard：成功路径返回数据并记一次成功。
    clock = FakeClock()
    inner = FakeAdapter([_market("fake", "m1")], name="fake")
    wrapped = ResilientAdapter(inner, clock=clock, sleep=RecordingSleep())

    seed = await wrapped.fetch_markets()
    refreshed = await wrapped.refresh_prices(seed)

    assert isinstance(refreshed, list)
    assert all(isinstance(m, CanonicalMarket) for m in refreshed)
    assert inner.refresh_calls == 1
    assert wrapped.metrics.success_count == 2  # fetch + refresh 各一次
