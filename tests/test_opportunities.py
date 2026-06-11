"""Unit tests for the OpportunityService wiring layer (Req 5.5, 6.1, 6.4).

These cover the reconciliation of ArbitrageEngine output into the
``OpportunityStore``: user-threshold filtering (Req 5.5), removal of
opportunities whose margin drops to <= 0 (Req 6.4), and the listing being
sorted by net margin descending (Req 6.1).

Validates: Property 10
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.models import ArbLeg, ArbitrageOpportunity
from scanner.opportunities import OpportunityService
from scanner.store import OpportunityStore

NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _opp(group_id: str, margin: float, size: float = 100.0) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=f"event {group_id}",
        legs=[ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4)],
        net_profit_margin=margin,
        recommended_size_usd=size,
        detected_at=NOW,
        data_age_seconds=1.0,
    )


# ---------------------------------------------------------------------------
# Threshold filtering (Req 5.5)
# ---------------------------------------------------------------------------

def test_below_threshold_opportunities_not_listed():
    service = OpportunityService(min_net_profit_margin=0.05)
    listed = service.reconcile([_opp("low", 0.02), _opp("ok", 0.10)])
    ids = [o.group_id for o in listed]
    assert ids == ["ok"]


def test_margin_at_threshold_is_listed():
    service = OpportunityService(min_net_profit_margin=0.05)
    listed = service.reconcile([_opp("at", 0.05)])
    assert [o.group_id for o in listed] == ["at"]


def test_zero_threshold_lists_all_positive_margins():
    service = OpportunityService(min_net_profit_margin=0.0)
    listed = service.reconcile([_opp("a", 0.01), _opp("b", 0.5)])
    assert {o.group_id for o in listed} == {"a", "b"}


# ---------------------------------------------------------------------------
# Removal on invalidation: margin <= 0 (Req 6.4, Property 10)
# ---------------------------------------------------------------------------

def test_zero_margin_opportunity_excluded():
    service = OpportunityService()
    listed = service.reconcile([_opp("zero", 0.0), _opp("pos", 0.1)])
    assert [o.group_id for o in listed] == ["pos"]


def test_negative_margin_opportunity_excluded():
    service = OpportunityService()
    listed = service.reconcile([_opp("neg", -0.2), _opp("pos", 0.1)])
    assert [o.group_id for o in listed] == ["pos"]


def test_listing_never_contains_non_positive_margin():
    # Property 10: no opportunity with net_profit_margin <= 0 is ever listed.
    service = OpportunityService()
    listed = service.reconcile(
        [_opp("a", 0.3), _opp("b", 0.0), _opp("c", -0.1), _opp("d", 0.05)]
    )
    assert all(o.net_profit_margin > 0 for o in listed)


def test_previously_listed_opportunity_removed_when_margin_drops():
    # An opportunity listed in cycle 1 must be removed once its margin drops <= 0.
    service = OpportunityService()
    service.reconcile([_opp("g1", 0.2)])
    assert service.store.get("g1") is not None

    listed = service.reconcile([_opp("g1", -0.05)])
    assert listed == []
    assert service.store.get("g1") is None


def test_opportunity_removed_when_group_disappears():
    service = OpportunityService()
    service.reconcile([_opp("g1", 0.2), _opp("g2", 0.3)])
    # Next cycle no longer reports g1 at all.
    listed = service.reconcile([_opp("g2", 0.3)])
    assert [o.group_id for o in listed] == ["g2"]
    assert service.store.get("g1") is None


def test_opportunity_removed_when_dropping_below_threshold_across_cycles():
    service = OpportunityService(min_net_profit_margin=0.1)
    service.reconcile([_opp("g1", 0.2)])
    assert service.store.get("g1") is not None
    # Margin falls below threshold (but still positive) -> de-listed and removed.
    listed = service.reconcile([_opp("g1", 0.05)])
    assert listed == []
    assert service.store.get("g1") is None


# ---------------------------------------------------------------------------
# Ordering (Req 6.1, Property 10)
# ---------------------------------------------------------------------------

def test_listing_sorted_by_net_margin_descending():
    service = OpportunityService()
    listed = service.reconcile(
        [_opp("low", 0.01), _opp("high", 0.25), _opp("mid", 0.10)]
    )
    assert [o.group_id for o in listed] == ["high", "mid", "low"]


def test_listing_sorted_after_filtering_mixed_input():
    service = OpportunityService(min_net_profit_margin=0.05)
    listed = service.reconcile(
        [
            _opp("neg", -0.1),
            _opp("below", 0.02),
            _opp("a", 0.20),
            _opp("zero", 0.0),
            _opp("b", 0.08),
            _opp("c", 0.50),
        ]
    )
    margins = [o.net_profit_margin for o in listed]
    assert margins == sorted(margins, reverse=True)
    assert [o.group_id for o in listed] == ["c", "a", "b"]


def test_list_reapplies_filter_defensively():
    # Even if the store is populated directly with a non-positive margin, the
    # service listing must not surface it (Property 10).
    store = OpportunityStore()
    store.upsert(_opp("sneaky", 0.0))
    store.upsert(_opp("ok", 0.1))
    service = OpportunityService(store=store)
    listed = service.list()
    assert [o.group_id for o in listed] == ["ok"]


# ---------------------------------------------------------------------------
# Updates across cycles
# ---------------------------------------------------------------------------

def test_reconcile_updates_existing_opportunity():
    service = OpportunityService()
    service.reconcile([_opp("g1", 0.2, size=100.0)])
    listed = service.reconcile([_opp("g1", 0.3, size=250.0)])
    assert len(listed) == 1
    assert listed[0].net_profit_margin == pytest.approx(0.3)
    assert listed[0].recommended_size_usd == pytest.approx(250.0)


def test_empty_cycle_clears_listing():
    service = OpportunityService()
    service.reconcile([_opp("g1", 0.2)])
    listed = service.reconcile([])
    assert listed == []
    assert len(service.store) == 0
