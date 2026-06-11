"""Unit tests for the PlatformAdapter boundary (Req 7.2).

Confirms AdapterError carries adapter context and that a conforming class
satisfies the runtime-checkable PlatformAdapter Protocol.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.models import CanonicalMarket, Outcome


def test_adapter_error_records_adapter_name():
    err = AdapterError("boom", adapter="kalshi")
    assert err.adapter == "kalshi"
    assert "boom" in str(err)


def test_adapter_error_defaults_adapter_to_none():
    err = AdapterError("boom")
    assert err.adapter is None


class _ConformingAdapter:
    name = "fake"

    async def fetch_markets(self) -> List[CanonicalMarket]:
        return [
            CanonicalMarket(
                platform=self.name,
                market_id="m1",
                title="Will X happen?",
                outcomes=[Outcome(name="YES", price=0.5)],
                retrieved_at=datetime.now(timezone.utc),
            )
        ]

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        return markets


def test_conforming_class_satisfies_protocol():
    assert isinstance(_ConformingAdapter(), PlatformAdapter)


def test_non_conforming_class_does_not_satisfy_protocol():
    class NotAnAdapter:
        name = "nope"

    assert not isinstance(NotAnAdapter(), PlatformAdapter)


async def test_conforming_adapter_fetch_returns_canonical_markets():
    adapter = _ConformingAdapter()
    markets = await adapter.fetch_markets()
    assert len(markets) == 1
    assert markets[0].platform == "fake"
    assert 0.0 <= markets[0].outcomes[0].price <= 1.0
