"""Reusable adapter conformance test suite (Req 7.2).

Any concrete :class:`~scanner.adapters.base.PlatformAdapter` must satisfy the
extensibility contract: it produces valid ``CanonicalMarket`` records and its
fetch is a cooperative coroutine that respects an externally imposed timeout.

This module is intentionally **not** named ``test_*`` so pytest does not collect
it directly. Concrete adapter test modules import :class:`AdapterConformanceTests`
and subclass it, overriding the ``adapter`` fixture (and optionally the
``slow_adapter`` fixture) to run the shared checks against their adapter:

    from tests.adapter_contract import AdapterConformanceTests, FakeAdapter

    class TestPolymarketConformance(AdapterConformanceTests):
        @pytest.fixture
        def adapter(self):
            return PolymarketAdapter(...)

The checks enforce:

- Property 1 (price bounds): every produced price/bid/ask is within [0, 1].
- Property 2 (non-negative magnitudes): volume/liquidity are None or >= 0.
- Property 3 (timestamp presence): every market has a ``retrieved_at`` and a
  non-negative ``age_seconds``.
- Req 7.2: an adapter conforming to the interface is ingestible without changes,
  and its fetch respects a fetch timeout (it is cancellable, not loop-blocking).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Sequence

import pytest

from scanner.adapters.base import PlatformAdapter
from scanner.models import CanonicalMarket, FieldStatus, Outcome


# --------------------------------------------------------------------------- #
# FakeAdapter: scripted adapter used to validate the conformance suite itself.
# --------------------------------------------------------------------------- #
class FakeAdapter:
    """A scripted :class:`PlatformAdapter` for tests.

    ``responses`` is a sequence of market batches returned on successive
    ``fetch_markets`` calls; once exhausted, the last batch is repeated. When
    ``responses`` is omitted, ``markets`` (or a single default market) is
    returned on every call.

    Injection hooks let tests exercise adapter-failure and timeout policy:

    - ``fetch_delay`` — ``await asyncio.sleep(fetch_delay)`` before returning,
      so the fetch yields control and can be cancelled by ``asyncio.wait_for``.
    - ``fetch_error`` — an exception instance raised by ``fetch_markets``.
    """

    def __init__(
        self,
        markets: Optional[List[CanonicalMarket]] = None,
        *,
        responses: Optional[Sequence[List[CanonicalMarket]]] = None,
        name: str = "fake",
        fetch_delay: float = 0.0,
        fetch_error: Optional[BaseException] = None,
    ) -> None:
        self.name = name
        self.fetch_delay = fetch_delay
        self.fetch_error = fetch_error
        self.fetch_calls = 0
        self.refresh_calls = 0
        if responses is not None:
            self._responses: List[List[CanonicalMarket]] = [list(b) for b in responses]
        elif markets is not None:
            self._responses = [list(markets)]
        else:
            self._responses = [[_sample_market()]]

    async def fetch_markets(self) -> List[CanonicalMarket]:
        if self.fetch_delay:
            await asyncio.sleep(self.fetch_delay)
        if self.fetch_error is not None:
            raise self.fetch_error
        index = min(self.fetch_calls, len(self._responses) - 1)
        self.fetch_calls += 1
        return list(self._responses[index])

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        self.refresh_calls += 1
        # Re-stamp retrieval time to mimic a fresh price read.
        refreshed: List[CanonicalMarket] = []
        for market in markets:
            refreshed.append(
                market.model_copy(update={"retrieved_at": datetime.now(timezone.utc)})
            )
        return refreshed


def _sample_market(
    *,
    platform: str = "fake",
    market_id: str = "m1",
    title: str = "Will X happen?",
) -> CanonicalMarket:
    """Build a representative, valid canonical market for the FakeAdapter."""
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=[
            Outcome(name="YES", price=0.6, bid=0.59, ask=0.61, available_liquidity_usd=1000.0),
            Outcome(name="NO", price=0.4, bid=0.39, ask=0.41, available_liquidity_usd=800.0),
        ],
        volume_usd=12345.0,
        liquidity_usd=5000.0,
        fee_rate=0.0,
        retrieved_at=datetime.now(timezone.utc),
        field_status={"volume_usd": FieldStatus.OK, "liquidity_usd": FieldStatus.OK},
    )


def sample_markets() -> List[CanonicalMarket]:
    """A small varied batch, including a market with unavailable magnitudes."""
    return [
        _sample_market(market_id="m1", title="Will X happen?"),
        CanonicalMarket(
            platform="fake",
            market_id="m2",
            title="Will Y happen?",
            outcomes=[Outcome(name="YES", price=0.0), Outcome(name="NO", price=1.0)],
            volume_usd=None,  # unavailable magnitude is allowed (Property 2)
            liquidity_usd=None,
            fee_rate=None,
            retrieved_at=datetime.now(timezone.utc),
            field_status={
                "volume_usd": FieldStatus.UNAVAILABLE,
                "liquidity_usd": FieldStatus.UNAVAILABLE,
            },
            unavailable_reasons={
                "volume_usd": "missing from source",
                "liquidity_usd": "missing from source",
            },
        ),
    ]


# --------------------------------------------------------------------------- #
# The reusable conformance suite.
# --------------------------------------------------------------------------- #
class AdapterConformanceTests:
    """Shared checks every PlatformAdapter must pass.

    Subclasses MUST override the ``adapter`` fixture. They MAY override the
    ``slow_adapter`` fixture to return an adapter whose ``fetch_markets`` blocks
    long enough to exercise the timeout check; when it returns ``None`` (the
    default) the timeout check is skipped.
    """

    @pytest.fixture
    def adapter(self) -> PlatformAdapter:
        raise NotImplementedError(
            "Subclasses of AdapterConformanceTests must override the `adapter` fixture"
        )

    @pytest.fixture
    def slow_adapter(self) -> Optional[PlatformAdapter]:
        return None

    # -- interface conformance ---------------------------------------------- #
    def test_satisfies_platform_adapter_protocol(self, adapter):
        assert isinstance(adapter, PlatformAdapter)

    def test_exposes_name(self, adapter):
        assert isinstance(adapter.name, str)
        assert adapter.name

    # -- fetch_markets produces valid canonical markets --------------------- #
    async def test_fetch_returns_list_of_canonical_markets(self, adapter):
        markets = await adapter.fetch_markets()
        assert isinstance(markets, list)
        for market in markets:
            assert isinstance(market, CanonicalMarket)

    async def test_fetch_prices_within_unit_interval(self, adapter):
        # Property 1: prices/bid/ask are implied probabilities in [0, 1].
        markets = await adapter.fetch_markets()
        for market in markets:
            for outcome in market.outcomes:
                assert 0.0 <= outcome.price <= 1.0
                if outcome.bid is not None:
                    assert 0.0 <= outcome.bid <= 1.0
                if outcome.ask is not None:
                    assert 0.0 <= outcome.ask <= 1.0

    async def test_fetch_magnitudes_non_negative(self, adapter):
        # Property 2: volume/liquidity are None/unavailable or >= 0.
        markets = await adapter.fetch_markets()
        for market in markets:
            if market.volume_usd is not None:
                assert market.volume_usd >= 0.0
            if market.liquidity_usd is not None:
                assert market.liquidity_usd >= 0.0
            for outcome in market.outcomes:
                if outcome.available_liquidity_usd is not None:
                    assert outcome.available_liquidity_usd >= 0.0

    async def test_fetch_sets_retrieval_timestamp(self, adapter):
        # Property 3: every market carries a retrieved_at with a sane age.
        markets = await adapter.fetch_markets()
        for market in markets:
            assert isinstance(market.retrieved_at, datetime)
            assert market.age_seconds >= 0.0

    async def test_fetch_sets_platform_identifier(self, adapter):
        # Req 2.1: a canonical market carries its platform identifier.
        markets = await adapter.fetch_markets()
        for market in markets:
            assert isinstance(market.platform, str)
            assert market.platform

    # -- refresh_prices keeps the contract ---------------------------------- #
    async def test_refresh_returns_canonical_markets(self, adapter):
        markets = await adapter.fetch_markets()
        refreshed = await adapter.refresh_prices(markets)
        assert isinstance(refreshed, list)
        for market in refreshed:
            assert isinstance(market, CanonicalMarket)
            for outcome in market.outcomes:
                assert 0.0 <= outcome.price <= 1.0

    # -- timeout policy (Req 7.2 / Req 1.5) --------------------------------- #
    async def test_fetch_respects_timeout(self, slow_adapter):
        """A slow fetch is cancellable via ``asyncio.wait_for``.

        This proves the adapter does cooperative async I/O (it yields control to
        the event loop) rather than blocking it, so the IngestionService's
        per-adapter ``asyncio.wait_for`` timeout (Req 1.5) can take effect.
        """
        if slow_adapter is None:
            pytest.skip("no slow_adapter fixture provided by this adapter test")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow_adapter.fetch_markets(), timeout=0.05)
