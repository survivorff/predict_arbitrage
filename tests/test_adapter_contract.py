"""Self-validation of the reusable adapter conformance suite (Req 7.2).

Runs the shared :class:`AdapterConformanceTests` against the scripted
:class:`FakeAdapter` to prove the suite is usable and passes for a conforming
adapter. Also includes negative tests asserting the suite's checks actually fail
for non-conforming adapters, so it cannot silently pass a bad adapter.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List, Optional

import pytest

from scanner.adapters.base import PlatformAdapter
from scanner.models import CanonicalMarket, Outcome
from tests.adapter_contract import (
    AdapterConformanceTests,
    FakeAdapter,
    sample_markets,
)


class TestFakeAdapterConformance(AdapterConformanceTests):
    """The FakeAdapter must itself satisfy the conformance contract."""

    @pytest.fixture
    def adapter(self) -> PlatformAdapter:
        return FakeAdapter(sample_markets(), name="fake")

    @pytest.fixture
    def slow_adapter(self) -> Optional[PlatformAdapter]:
        # A fetch that sleeps far longer than the test's wait_for timeout.
        return FakeAdapter(fetch_delay=5.0, name="slow-fake")


# --- direct behavioral checks on FakeAdapter -------------------------------- #

def test_fake_adapter_is_a_platform_adapter():
    assert isinstance(FakeAdapter(), PlatformAdapter)


async def test_fake_adapter_returns_default_market():
    adapter = FakeAdapter()
    markets = await adapter.fetch_markets()
    assert len(markets) == 1
    assert markets[0].platform == "fake"


async def test_fake_adapter_replays_scripted_responses_in_order():
    batch_a = [
        CanonicalMarket(
            platform="fake",
            market_id="a",
            title="A",
            outcomes=[Outcome(name="YES", price=0.5)],
            retrieved_at=datetime.now(timezone.utc),
        )
    ]
    batch_b = [
        CanonicalMarket(
            platform="fake",
            market_id="b",
            title="B",
            outcomes=[Outcome(name="YES", price=0.7)],
            retrieved_at=datetime.now(timezone.utc),
        )
    ]
    adapter = FakeAdapter(responses=[batch_a, batch_b])

    first = await adapter.fetch_markets()
    second = await adapter.fetch_markets()
    third = await adapter.fetch_markets()  # exhausted -> repeats last batch

    assert first[0].market_id == "a"
    assert second[0].market_id == "b"
    assert third[0].market_id == "b"
    assert adapter.fetch_calls == 3


async def test_fake_adapter_raises_scripted_error():
    adapter = FakeAdapter(fetch_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await adapter.fetch_markets()


async def test_fake_adapter_refresh_restamps_timestamp():
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    market = CanonicalMarket(
        platform="fake",
        market_id="m1",
        title="A",
        outcomes=[Outcome(name="YES", price=0.5)],
        retrieved_at=old,
    )
    adapter = FakeAdapter()
    refreshed = await adapter.refresh_prices([market])
    assert refreshed[0].retrieved_at > old
    assert adapter.refresh_calls == 1


async def test_slow_fake_adapter_is_cancellable_by_wait_for():
    adapter = FakeAdapter(fetch_delay=5.0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter.fetch_markets(), timeout=0.05)


# --- the suite must reject non-conforming adapters -------------------------- #

class _BadPriceAdapter:
    """Returns a market whose model would reject the price; we bypass the model
    to simulate an adapter that emits an out-of-range price via a stub object."""

    name = "bad"

    async def fetch_markets(self):
        class _StubOutcome:
            price = 1.5
            bid = None
            ask = None
            available_liquidity_usd = None

        class _StubMarket:
            platform = "bad"
            outcomes = [_StubOutcome()]
            volume_usd = None
            liquidity_usd = None
            retrieved_at = datetime.now(timezone.utc)

            @property
            def age_seconds(self):
                return 0.0

        return [_StubMarket()]

    async def refresh_prices(self, markets):
        return markets


async def test_suite_price_check_fails_for_out_of_range_price():
    """The price-bounds check must fail when an adapter emits a bad price."""
    suite = AdapterConformanceTests()
    adapter = _BadPriceAdapter()
    with pytest.raises(AssertionError):
        await suite.test_fetch_prices_within_unit_interval(adapter)


async def test_suite_skips_timeout_check_without_slow_adapter():
    """When no slow adapter is provided, the timeout check is skipped, not failed."""
    suite = AdapterConformanceTests()
    with pytest.raises(pytest.skip.Exception):
        # slow_adapter default is None
        await suite.test_fetch_respects_timeout(None)
