"""Unit tests for the IngestionService (Req 1.1-1.6, 8.2, 8.3).

Time is injected via a deterministic clock and adapters are scripted
``FakeAdapter`` instances so the suite runs offline and deterministically.

Covers the task's correctness properties:
- Property 3 (Timestamp presence): every ingested record carries the injected
  ``retrieved_at``.
- Property 4 (Failure isolation): one adapter failing or timing out does not
  block the others in the same cycle.
- Property 5 (Staleness exclusion, ingestion half): records whose age exceeds
  the staleness threshold are flagged ``is_stale`` so the ArbitrageEngine can
  later exclude them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from scanner.adapters.base import AdapterError
from scanner.ingestion import IngestionService, MAX_REFRESH_INTERVAL_SECONDS
from scanner.models import CanonicalMarket, Outcome
from scanner.store import InMemoryMarketStore
from tests.adapter_contract import FakeAdapter

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic, advanceable UTC clock for injection."""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _market(
    platform: str,
    market_id: str,
    *,
    retrieved_at: datetime = BASE_TIME,
    price: float = 0.6,
) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=f"{platform}:{market_id}",
        outcomes=[Outcome(name="YES", price=price), Outcome(name="NO", price=1 - price)],
        volume_usd=1000.0,
        liquidity_usd=500.0,
        fee_rate=0.0,
        retrieved_at=retrieved_at,
    )


# --------------------------------------------------------------------------- #
# Construction / configuration guards (Req 1.3)
# --------------------------------------------------------------------------- #

def test_rejects_refresh_interval_above_60_seconds():
    # Req 1.3: refresh interval must be <= 60 seconds.
    with pytest.raises(ValueError):
        IngestionService([], InMemoryMarketStore(), refresh_interval=61)


def test_accepts_refresh_interval_at_the_60_second_boundary():
    service = IngestionService(
        [], InMemoryMarketStore(), refresh_interval=MAX_REFRESH_INTERVAL_SECONDS
    )
    assert service.refresh_interval == MAX_REFRESH_INTERVAL_SECONDS


def test_rejects_non_positive_refresh_interval():
    with pytest.raises(ValueError):
        IngestionService([], InMemoryMarketStore(), refresh_interval=0)


def test_rejects_non_positive_fetch_timeout():
    with pytest.raises(ValueError):
        IngestionService([], InMemoryMarketStore(), fetch_timeout=0)


# --------------------------------------------------------------------------- #
# Cycle: fetch -> normalize -> timestamp -> store (Req 1.1, 1.2, 1.4)
# --------------------------------------------------------------------------- #

async def test_cycle_stores_markets_from_adapter():
    # Req 1.1/1.2: retrieve and normalize into the store.
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService([adapter], store, clock=FakeClock())

    await service._cycle(adapter)

    assert store.get("fake", "m1") is not None
    assert len(store) == 1


async def test_cycle_stamps_injected_retrieved_at_on_success():
    # Req 1.4 / Property 3: every ingested record carries the retrieval time.
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    # Adapter returns a record with an unrelated (old) timestamp; the service
    # must overwrite it with the clock's "now".
    stale_stamp = BASE_TIME - timedelta(hours=5)
    adapter = FakeAdapter([_market("fake", "m1", retrieved_at=stale_stamp)], name="fake")
    service = IngestionService([adapter], store, clock=clock)

    await service._cycle(adapter)

    stored = store.get("fake", "m1")
    assert stored.retrieved_at == BASE_TIME


# --------------------------------------------------------------------------- #
# Failure isolation (Req 1.5, Property 4)
# --------------------------------------------------------------------------- #

async def test_adapter_error_is_isolated_and_other_adapters_still_ingest():
    # Property 4: a failing adapter does not block the others.
    store = InMemoryMarketStore()
    failing = FakeAdapter(
        name="bad", fetch_error=AdapterError("boom", adapter="bad")
    )
    healthy = FakeAdapter([_market("good", "m1")], name="good")
    service = IngestionService([failing, healthy], store, clock=FakeClock())

    await service._cycle(failing)
    await service._cycle(healthy)

    assert store.get("good", "m1") is not None
    assert store.get("bad", "m1") is None


async def test_adapter_timeout_is_isolated(caplog):
    # Req 1.5: a slow adapter times out, is logged with its name, and the loop
    # continues. fetch_timeout is tiny so the test is fast and deterministic.
    store = InMemoryMarketStore()
    slow = FakeAdapter([_market("slow", "m1")], name="slow", fetch_delay=10)
    service = IngestionService([slow], store, fetch_timeout=0.01, clock=FakeClock())

    with caplog.at_level("WARNING"):
        await service._cycle(slow)

    assert len(store) == 0
    assert "slow" in caplog.text


async def test_failure_retains_last_good_data():
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService([adapter], store, clock=FakeClock())

    await service._cycle(adapter)  # succeeds, stores m1
    assert store.get("fake", "m1") is not None

    # Now make the same adapter fail and run another cycle.
    adapter.fetch_error = AdapterError("now broken", adapter="fake")
    await service._cycle(adapter)

    # The previously stored good data is still present.
    assert store.get("fake", "m1") is not None


async def test_disappeared_markets_are_pruned_across_cycles():
    # 稳定性修复：某市场本轮不再返回应从 store 删除，避免陈旧幽灵累积。
    store = InMemoryMarketStore()
    adapter = FakeAdapter(
        name="fake",
        responses=[
            [_market("fake", "m1"), _market("fake", "m2")],  # 第 1 轮：m1, m2
            [_market("fake", "m1"), _market("fake", "m3")],  # 第 2 轮：m1, m3（m2 消失）
        ],
    )
    service = IngestionService([adapter], store, clock=FakeClock())

    await service._cycle(adapter)
    assert {m.market_id for m in store.list_by_platform("fake")} == {"m1", "m2"}

    await service._cycle(adapter)
    # m2 消失被清除，m3 加入；不累积幽灵。
    assert {m.market_id for m in store.list_by_platform("fake")} == {"m1", "m3"}


async def test_failed_fetch_does_not_prune_existing_markets():
    # 抓取失败时保留上一份好数据（不应误删），仅成功抓取才替换。
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService([adapter], store, clock=FakeClock())

    await service._cycle(adapter)
    adapter.fetch_error = AdapterError("down", adapter="fake")
    await service._cycle(adapter)
    # 失败周期不触发 replace_platform，旧市场仍在。
    assert store.get("fake", "m1") is not None


async def test_unexpected_exception_is_isolated():
    store = InMemoryMarketStore()
    boom = FakeAdapter(name="boom", fetch_error=RuntimeError("unexpected"))
    healthy = FakeAdapter([_market("good", "m1")], name="good")
    service = IngestionService([boom, healthy], store, clock=FakeClock())

    await service._cycle(boom)  # must not raise
    await service._cycle(healthy)

    assert store.get("good", "m1") is not None


# --------------------------------------------------------------------------- #
# Staleness recomputation (Req 1.6, 8.2, 8.3, Property 5)
# --------------------------------------------------------------------------- #

async def test_fresh_record_is_not_flagged_stale():
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService(
        [adapter], store, staleness_threshold=60, clock=clock
    )

    await service._cycle(adapter)
    assert store.get("fake", "m1").is_stale is False


async def test_record_flagged_stale_when_age_exceeds_threshold():
    # Req 1.6 / 8.2 / Property 5: data older than the threshold is flagged stale.
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService(
        [adapter], store, staleness_threshold=60, clock=clock
    )

    await service._cycle(adapter)  # stamped at BASE_TIME, fresh
    assert store.get("fake", "m1").is_stale is False

    # Advance the clock past the threshold without a successful refresh.
    clock.advance(61)
    adapter.fetch_error = AdapterError("down", adapter="fake")
    await service._cycle(adapter)  # fetch fails but staleness still recomputed

    assert store.get("fake", "m1").is_stale is True


async def test_stale_record_becomes_fresh_after_successful_refresh():
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService(
        [adapter], store, staleness_threshold=60, clock=clock
    )

    await service._cycle(adapter)
    clock.advance(120)
    adapter.fetch_error = AdapterError("down", adapter="fake")
    await service._cycle(adapter)
    assert store.get("fake", "m1").is_stale is True

    # Recover: a successful fetch re-stamps retrieved_at to "now".
    adapter.fetch_error = None
    await service._cycle(adapter)
    assert store.get("fake", "m1").is_stale is False


async def test_default_staleness_threshold_is_60_seconds():
    # Req 8.3: default staleness threshold is 60s.
    service = IngestionService([], InMemoryMarketStore())
    assert service.staleness_threshold == 60


async def test_exactly_at_threshold_is_not_stale():
    # Strictly greater than the threshold is stale; equal is still fresh.
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService(
        [adapter], store, staleness_threshold=60, clock=clock
    )

    await service._cycle(adapter)
    clock.advance(60)
    adapter.fetch_error = AdapterError("down", adapter="fake")
    await service._cycle(adapter)

    assert store.get("fake", "m1").is_stale is False


# --------------------------------------------------------------------------- #
# The run() loop with one task per adapter (Req 1.1, 1.5)
# --------------------------------------------------------------------------- #

async def test_run_with_no_adapters_returns_immediately():
    service = IngestionService([], InMemoryMarketStore())
    await asyncio.wait_for(service.run(), timeout=1.0)


async def test_run_drives_each_adapter_and_isolates_failures(caplog):
    # Property 4 at the loop level: one failing adapter loop does not stop the
    # other adapter's loop from ingesting across cycles.
    clock = FakeClock(BASE_TIME)
    store = InMemoryMarketStore()
    failing = FakeAdapter(name="bad", fetch_error=AdapterError("boom"))
    healthy = FakeAdapter([_market("good", "m1")], name="good")
    service = IngestionService(
        [failing, healthy], store, refresh_interval=0.01, clock=clock
    )

    with caplog.at_level("WARNING"):
        task = asyncio.ensure_future(service.run())
        # Let several cycles run, then cancel the long-running loops.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The healthy adapter ingested despite the other adapter's loop failing,
    # which itself logged its failure with its own name.
    assert store.get("good", "m1") is not None
    assert healthy.fetch_calls >= 1
    assert "bad" in caplog.text


async def test_run_refreshes_repeatedly_over_multiple_cycles():
    # Req 1.3: the adapter is polled repeatedly on the interval.
    store = InMemoryMarketStore()
    adapter = FakeAdapter([_market("fake", "m1")], name="fake")
    service = IngestionService(
        [adapter], store, refresh_interval=0.01, clock=FakeClock()
    )

    task = asyncio.ensure_future(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.fetch_calls >= 2
