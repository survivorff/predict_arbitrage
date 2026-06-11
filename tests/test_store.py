"""Unit tests for the in-memory market and opportunity stores.

Covers upsert/get/list behavior for ``InMemoryMarketStore`` and the
sorting/removal behavior of ``OpportunityStore`` (Req 6.1, 7.4; Property 10).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.models import ArbLeg, ArbitrageOpportunity, CanonicalMarket, Outcome
from scanner.store import InMemoryMarketStore, MarketStore, OpportunityStore


def _market(platform: str, market_id: str, **overrides) -> CanonicalMarket:
    base = dict(
        platform=platform,
        market_id=market_id,
        title=f"{platform}:{market_id}",
        outcomes=[Outcome(name="YES", price=0.6), Outcome(name="NO", price=0.4)],
        volume_usd=1000.0,
        liquidity_usd=500.0,
        fee_rate=0.0,
        retrieved_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return CanonicalMarket(**base)


def _opp(group_id: str, margin: float) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=f"event {group_id}",
        legs=[ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4)],
        net_profit_margin=margin,
        recommended_size_usd=100.0,
        detected_at=datetime.now(timezone.utc),
        data_age_seconds=1.0,
    )


# --- InMemoryMarketStore: interface conformance -----------------------------

def test_inmemory_store_satisfies_protocol():
    store = InMemoryMarketStore()
    assert isinstance(store, MarketStore)


# --- InMemoryMarketStore: upsert / get --------------------------------------

def test_upsert_then_get_returns_market():
    store = InMemoryMarketStore()
    m = _market("polymarket", "m1")
    store.upsert([m])
    assert store.get("polymarket", "m1") is m


def test_get_missing_key_returns_none():
    store = InMemoryMarketStore()
    assert store.get("kalshi", "nope") is None


def test_get_distinguishes_same_id_across_platforms():
    store = InMemoryMarketStore()
    pm = _market("polymarket", "shared")
    kal = _market("kalshi", "shared")
    store.upsert([pm, kal])
    assert store.get("polymarket", "shared") is pm
    assert store.get("kalshi", "shared") is kal
    assert len(store) == 2


def test_upsert_replaces_existing_market_for_same_key():
    store = InMemoryMarketStore()
    first = _market("polymarket", "m1", volume_usd=100.0)
    second = _market("polymarket", "m1", volume_usd=999.0)
    store.upsert([first])
    store.upsert([second])
    assert len(store) == 1
    assert store.get("polymarket", "m1").volume_usd == 999.0


def test_upsert_accepts_multiple_markets_at_once():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "a"), _market("polymarket", "b")])
    assert len(store) == 2


def test_upsert_empty_iterable_is_noop():
    store = InMemoryMarketStore()
    store.upsert([])
    assert len(store) == 0


# --- InMemoryMarketStore: listing -------------------------------------------

def test_list_all_returns_every_market():
    store = InMemoryMarketStore()
    markets = [_market("polymarket", "a"), _market("kalshi", "b")]
    store.upsert(markets)
    assert set(id(m) for m in store.list_all()) == set(id(m) for m in markets)


def test_list_all_empty_store():
    assert InMemoryMarketStore().list_all() == []


def test_list_by_platform_filters_by_platform():
    store = InMemoryMarketStore()
    pm_a = _market("polymarket", "a")
    pm_b = _market("polymarket", "b")
    kal = _market("kalshi", "c")
    store.upsert([pm_a, pm_b, kal])

    pm_markets = store.list_by_platform("polymarket")
    assert len(pm_markets) == 2
    assert all(m.platform == "polymarket" for m in pm_markets)
    assert store.list_by_platform("kalshi") == [kal]


def test_list_by_platform_unknown_platform_returns_empty():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "a")])
    assert store.list_by_platform("unknown") == []


# --- InMemoryMarketStore: replace_platform (清理消失的市场) ------------------

def test_replace_platform_adds_and_removes():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "a"), _market("polymarket", "b")])
    store.replace_platform("polymarket", [_market("polymarket", "a"), _market("polymarket", "c")])
    ids = {m.market_id for m in store.list_by_platform("polymarket")}
    assert ids == {"a", "c"}


def test_replace_platform_does_not_touch_other_platforms():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "a"), _market("predictfun", "x")])
    store.replace_platform("polymarket", [_market("polymarket", "b")])
    assert {m.market_id for m in store.list_by_platform("polymarket")} == {"b"}
    assert {m.market_id for m in store.list_by_platform("predictfun")} == {"x"}


def test_replace_platform_empty_clears_platform():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "a"), _market("polymarket", "b")])
    store.replace_platform("polymarket", [])
    assert store.list_by_platform("polymarket") == []


# --- OpportunityStore: upsert / get / remove --------------------------------

def test_opportunity_upsert_then_get():
    store = OpportunityStore()
    opp = _opp("g1", 0.1)
    store.upsert(opp)
    assert store.get("g1") is opp


def test_opportunity_upsert_replaces_by_group_id():
    store = OpportunityStore()
    store.upsert(_opp("g1", 0.1))
    store.upsert(_opp("g1", 0.2))
    assert len(store) == 1
    assert store.get("g1").net_profit_margin == 0.2


def test_opportunity_remove_by_group_id():
    store = OpportunityStore()
    opp = _opp("g1", 0.1)
    store.upsert(opp)
    removed = store.remove("g1")
    assert removed is opp
    assert store.get("g1") is None
    assert len(store) == 0


def test_opportunity_remove_missing_returns_none():
    store = OpportunityStore()
    assert store.remove("nope") is None


# --- OpportunityStore: sorting (Req 6.1, Property 10) ------------------------

def test_list_sorted_orders_by_net_margin_descending():
    store = OpportunityStore()
    store.upsert(_opp("low", 0.01))
    store.upsert(_opp("high", 0.25))
    store.upsert(_opp("mid", 0.10))

    margins = [o.net_profit_margin for o in store.list_sorted()]
    assert margins == [0.25, 0.10, 0.01]


def test_list_sorted_empty_store():
    assert OpportunityStore().list_sorted() == []


def test_list_sorted_is_descending_for_arbitrary_margins():
    store = OpportunityStore()
    for i, margin in enumerate([0.3, -0.1, 0.0, 0.15, 0.05]):
        store.upsert(_opp(f"g{i}", margin))
    margins = [o.net_profit_margin for o in store.list_sorted()]
    assert margins == sorted(margins, reverse=True)
