"""Unit tests for the RankingService.

Covers descending sort order by volume and liquidity (Req 3.1-3.3), threshold
filtering by ``min_volume``/``min_liquidity`` (Req 3.4-3.5), and placement of
markets whose ranking metric is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.models import CanonicalMarket, Outcome
from scanner.ranking import RankingService


def _market(market_id: str, *, volume=None, liquidity=None) -> CanonicalMarket:
    return CanonicalMarket(
        platform="polymarket",
        market_id=market_id,
        title=f"market {market_id}",
        outcomes=[Outcome(name="YES", price=0.5), Outcome(name="NO", price=0.5)],
        volume_usd=volume,
        liquidity_usd=liquidity,
        fee_rate=0.0,
        retrieved_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def service() -> RankingService:
    return RankingService()


# --- Sort order (Req 3.1-3.3) -----------------------------------------------

def test_rank_by_volume_descending(service):
    markets = [
        _market("low", volume=100.0),
        _market("high", volume=900.0),
        _market("mid", volume=500.0),
    ]
    ranked = service.rank(markets, by="volume")
    assert [m.market_id for m in ranked] == ["high", "mid", "low"]


def test_rank_by_liquidity_descending(service):
    markets = [
        _market("a", liquidity=10.0),
        _market("b", liquidity=30.0),
        _market("c", liquidity=20.0),
    ]
    ranked = service.rank(markets, by="liquidity")
    assert [m.market_id for m in ranked] == ["b", "c", "a"]


def test_rank_does_not_mutate_input(service):
    markets = [_market("a", volume=1.0), _market("b", volume=2.0)]
    original = list(markets)
    service.rank(markets, by="volume")
    assert markets == original


def test_rank_empty_list(service):
    assert service.rank([], by="volume") == []


def test_rank_invalid_criterion_raises(service):
    with pytest.raises(ValueError):
        service.rank([], by="price")  # type: ignore[arg-type]


# --- Threshold filtering (Req 3.4-3.5) --------------------------------------

def test_min_volume_excludes_below_threshold(service):
    markets = [
        _market("keep", volume=500.0),
        _market("drop", volume=100.0),
        _market("edge", volume=200.0),
    ]
    ranked = service.rank(markets, by="volume", min_volume=200.0)
    # Threshold is inclusive: 200 is kept, 100 is dropped.
    assert [m.market_id for m in ranked] == ["keep", "edge"]


def test_min_liquidity_excludes_below_threshold(service):
    markets = [
        _market("keep", liquidity=500.0),
        _market("drop", liquidity=50.0),
    ]
    ranked = service.rank(markets, by="liquidity", min_liquidity=100.0)
    assert [m.market_id for m in ranked] == ["keep"]


def test_both_thresholds_applied_together(service):
    markets = [
        _market("keep", volume=500.0, liquidity=500.0),
        _market("low_vol", volume=50.0, liquidity=500.0),
        _market("low_liq", volume=500.0, liquidity=50.0),
    ]
    ranked = service.rank(
        markets, by="volume", min_volume=100.0, min_liquidity=100.0
    )
    assert [m.market_id for m in ranked] == ["keep"]


def test_threshold_excludes_unavailable_metric(service):
    markets = [
        _market("has_volume", volume=300.0),
        _market("no_volume", volume=None),
    ]
    ranked = service.rank(markets, by="volume", min_volume=100.0)
    assert [m.market_id for m in ranked] == ["has_volume"]


# --- Unavailable-metric placement -------------------------------------------

def test_unavailable_metric_sorts_last(service):
    markets = [
        _market("none1", volume=None),
        _market("high", volume=900.0),
        _market("none2", volume=None),
        _market("low", volume=100.0),
    ]
    ranked = service.rank(markets, by="volume")
    ids = [m.market_id for m in ranked]
    # Markets with a value come first, descending; unavailable ones come last.
    assert ids[:2] == ["high", "low"]
    assert set(ids[2:]) == {"none1", "none2"}


def test_all_unavailable_metrics_returned(service):
    markets = [_market("a", volume=None), _market("b", volume=None)]
    ranked = service.rank(markets, by="volume")
    assert {m.market_id for m in ranked} == {"a", "b"}


def test_liquidity_unavailable_sorts_last_when_ranking_by_liquidity(service):
    markets = [
        _market("none", liquidity=None, volume=999.0),
        _market("has", liquidity=10.0, volume=1.0),
    ]
    ranked = service.rank(markets, by="liquidity")
    assert [m.market_id for m in ranked] == ["has", "none"]
