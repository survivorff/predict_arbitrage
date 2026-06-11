"""Unit tests for canonical data models.

Covers Property 1 (price bounds, Req 2.2) and Property 2 (non-negative
magnitudes, Req 2.3), plus structural validators on the downstream models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from scanner.models import (
    ArbLeg,
    ArbitrageOpportunity,
    CanonicalMarket,
    EquivalentMarketGroup,
    FieldStatus,
    Outcome,
    OutcomeAlignment,
)


def _market(**overrides) -> dict:
    base = dict(
        platform="polymarket",
        market_id="m1",
        title="Will X happen?",
        outcomes=[Outcome(name="YES", price=0.6), Outcome(name="NO", price=0.4)],
        volume_usd=1000.0,
        liquidity_usd=500.0,
        fee_rate=0.0,
        retrieved_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return base


# --- Property 1: price bounds (Req 2.2) -------------------------------------

def test_outcome_accepts_valid_prices():
    o = Outcome(name="YES", price=0.0, bid=0.5, ask=1.0)
    assert o.price == 0.0
    assert o.bid == 0.5
    assert o.ask == 1.0


@pytest.mark.parametrize("bad_price", [-0.01, 1.01, -5, 2.0])
def test_outcome_rejects_out_of_range_price(bad_price):
    with pytest.raises(ValidationError):
        Outcome(name="YES", price=bad_price)


@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("bad_value", [-0.1, 1.5])
def test_outcome_rejects_out_of_range_bid_ask(field, bad_value):
    with pytest.raises(ValidationError):
        Outcome(**{"name": "YES", "price": 0.5, field: bad_value})


def test_arbleg_rejects_out_of_range_price():
    with pytest.raises(ValidationError):
        ArbLeg(platform="kalshi", market_id="k1", outcome="YES", price=1.2)


# --- Property 2: non-negative magnitudes (Req 2.3) --------------------------

def test_market_accepts_valid_magnitudes():
    m = CanonicalMarket(**_market(volume_usd=0.0, liquidity_usd=12345.6))
    assert m.volume_usd == 0.0
    assert m.liquidity_usd == 12345.6


@pytest.mark.parametrize("field", ["volume_usd", "liquidity_usd"])
def test_market_rejects_negative_magnitudes(field):
    with pytest.raises(ValidationError):
        CanonicalMarket(**_market(**{field: -1.0}))


def test_market_allows_none_magnitudes():
    m = CanonicalMarket(**_market(volume_usd=None, liquidity_usd=None))
    assert m.volume_usd is None
    assert m.liquidity_usd is None


def test_outcome_rejects_negative_liquidity():
    with pytest.raises(ValidationError):
        Outcome(name="YES", price=0.5, available_liquidity_usd=-10.0)


# --- age_seconds / freshness (Req 8.1, Property 3) --------------------------

def test_age_seconds_increases_with_older_timestamp():
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    m = CanonicalMarket(**_market(retrieved_at=old))
    assert m.age_seconds >= 120


def test_age_seconds_handles_naive_timestamp_as_utc():
    naive = datetime.utcnow() - timedelta(seconds=30)
    m = CanonicalMarket(**_market(retrieved_at=naive))
    assert m.age_seconds >= 30


# --- field status / unavailable reasons (Req 2.4) ---------------------------

def test_field_status_and_reasons_recorded():
    m = CanonicalMarket(
        **_market(
            volume_usd=None,
            field_status={"volume_usd": FieldStatus.UNAVAILABLE},
            unavailable_reasons={"volume_usd": "missing from source"},
        )
    )
    assert m.field_status["volume_usd"] == FieldStatus.UNAVAILABLE
    assert m.unavailable_reasons["volume_usd"] == "missing from source"


# --- downstream models ------------------------------------------------------

def test_market_rejects_out_of_range_fee_rate():
    with pytest.raises(ValidationError):
        CanonicalMarket(**_market(fee_rate=1.5))


@pytest.mark.parametrize("bad_conf", [-0.1, 1.1])
def test_group_rejects_out_of_range_confidence(bad_conf):
    with pytest.raises(ValidationError):
        EquivalentMarketGroup(group_id="g1", members=[], match_confidence=bad_conf)


def test_group_accepts_valid_confidence_and_outcome_map():
    alignment = OutcomeAlignment(
        canonical_outcome="YES",
        platform_outcomes={"polymarket": "YES", "kalshi": "NO"},
        inverted={"polymarket": False, "kalshi": True},
    )
    g = EquivalentMarketGroup(group_id="g1", members=[], outcome_map=[alignment], match_confidence=0.85)
    assert g.match_confidence == 0.85
    assert g.outcome_map[0].inverted["kalshi"] is True


def test_arbitrage_opportunity_rejects_negative_size():
    with pytest.raises(ValidationError):
        ArbitrageOpportunity(
            group_id="g1",
            event_title="Will X happen?",
            legs=[ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4)],
            net_profit_margin=0.05,
            recommended_size_usd=-100.0,
            detected_at=datetime.now(timezone.utc),
            data_age_seconds=1.0,
        )


def test_arbitrage_opportunity_accepts_valid_input():
    opp = ArbitrageOpportunity(
        group_id="g1",
        event_title="Will X happen?",
        legs=[
            ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4),
            ArbLeg(platform="kalshi", market_id="k1", outcome="NO", price=0.5),
        ],
        net_profit_margin=0.11,
        recommended_size_usd=250.0,
        detected_at=datetime.now(timezone.utc),
        data_age_seconds=2.0,
    )
    assert opp.net_profit_margin == 0.11
    assert len(opp.legs) == 2
