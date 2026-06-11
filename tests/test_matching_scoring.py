"""Unit tests for the scoring tier of the MatchingEngine (Req 4.2-4.6).

Covers composite similarity scoring, outcome mapping (including inverted
YES/NO), confidence range, grouping, and exclusion of markets whose outcomes
cannot all be mapped. Uses labeled true-match and hard-negative fixtures.

**Validates: Requirements 4.2, 4.3, 4.4, 4.6**
**Validates: Property 6, Property 7**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from scanner.matching import (
    MatchingEngine,
    ScoreWeights,
    composite_score,
    map_outcomes,
)
from scanner.matching import LexicalSimilarity
from scanner.models import CanonicalMarket, Outcome

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def outcome(name: str, price: float = 0.5) -> Outcome:
    return Outcome(name=name, price=price)


def market(
    platform: str,
    market_id: str,
    title: str,
    outcomes: Optional[List[Outcome]] = None,
) -> CanonicalMarket:
    if outcomes is None:
        outcomes = [outcome("Yes", 0.5), outcome("No", 0.5)]
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=outcomes,
        retrieved_at=NOW,
    )


# --- Labeled fixtures -------------------------------------------------------


def true_match_pair():
    """Same real-world event phrased differently across two platforms."""
    poly = market(
        "polymarket",
        "p_btc",
        "Will Bitcoin close above 100000 in 2025",
    )
    kalshi = market(
        "kalshi",
        "k_btc",
        "Bitcoin above 100000 by 2025",
    )
    return poly, kalshi


def hard_negative_pair():
    """Two different date/threshold-bounded markets that share some tokens."""
    poly = market(
        "polymarket",
        "p_btc_2025",
        "Will Bitcoin close above 100000 in 2025",
    )
    kalshi = market(
        "kalshi",
        "k_eth_2030",
        "Will Ethereum exceed 5000 in 2030",
    )
    return poly, kalshi


# --- composite_score --------------------------------------------------------


def test_composite_score_in_unit_interval_for_true_match():
    a, b = true_match_pair()
    score = composite_score(a, b, similarity=LexicalSimilarity())
    assert 0.0 <= score <= 1.0


def test_composite_score_true_match_above_hard_negative():
    sim = LexicalSimilarity()
    match_a, match_b = true_match_pair()
    neg_a, neg_b = hard_negative_pair()
    match_score = composite_score(match_a, match_b, similarity=sim)
    neg_score = composite_score(neg_a, neg_b, similarity=sim)
    assert match_score > neg_score


def test_composite_score_identical_markets_high():
    sim = LexicalSimilarity()
    a = market("polymarket", "p1", "Lakers win the 2025 NBA championship")
    b = market("kalshi", "k1", "Lakers win the 2025 NBA championship")
    assert composite_score(a, b, similarity=sim) >= 0.8


def test_composite_score_always_within_bounds_extreme_weights():
    sim = LexicalSimilarity()
    a, b = true_match_pair()
    weights = ScoreWeights(title=10.0, entity_overlap=0.0, structure=0.0, date_proximity=0.0)
    score = composite_score(a, b, similarity=sim, weights=weights)
    assert 0.0 <= score <= 1.0


# --- map_outcomes -----------------------------------------------------------


def test_map_outcomes_aligns_yes_yes_no_no():
    a, b = true_match_pair()
    alignments = map_outcomes([a, b])
    assert alignments is not None
    canon = {al.canonical_outcome: al for al in alignments}
    assert set(canon) == {"YES", "NO"}

    yes = canon["YES"]
    assert yes.platform_outcomes["polymarket"] == "Yes"
    assert yes.platform_outcomes["kalshi"] == "Yes"
    assert yes.inverted["polymarket"] is False
    assert yes.inverted["kalshi"] is False

    no = canon["NO"]
    assert no.platform_outcomes["polymarket"] == "No"
    assert no.platform_outcomes["kalshi"] == "No"


def test_map_outcomes_handles_inverted_negation_market():
    # Platform B phrases the question as the negation ("NOT win").
    a = market("polymarket", "p1", "Will Trump win the 2024 election")
    b = market("kalshi", "k1", "Will Trump not win the 2024 election")
    alignments = map_outcomes([a, b])
    assert alignments is not None
    canon = {al.canonical_outcome: al for al in alignments}

    yes = canon["YES"]
    # A is straightforward; B is inverted so canonical YES maps to B's "No".
    assert yes.inverted["polymarket"] is False
    assert yes.inverted["kalshi"] is True
    assert yes.platform_outcomes["polymarket"] == "Yes"
    assert yes.platform_outcomes["kalshi"] == "No"

    no = canon["NO"]
    assert no.platform_outcomes["kalshi"] == "Yes"


def test_map_outcomes_returns_none_when_outcome_unmapped():
    # A non-binary / unmappable outcome set cannot be fully mapped (Req 4.6).
    a = market("polymarket", "p1", "Election winner")
    bad = market(
        "kalshi",
        "k1",
        "Election winner",
        outcomes=[outcome("Candidate A"), outcome("Candidate B")],
    )
    assert map_outcomes([a, bad]) is None


def test_map_outcomes_returns_none_for_three_way_market():
    a = market("polymarket", "p1", "Match result")
    three_way = market(
        "kalshi",
        "k1",
        "Match result",
        outcomes=[outcome("Home"), outcome("Draw"), outcome("Away")],
    )
    assert map_outcomes([a, three_way]) is None


# --- MatchingEngine.match ---------------------------------------------------


def test_match_groups_true_match_pair():
    a, b = true_match_pair()
    groups = MatchingEngine().match([a, b])
    assert len(groups) == 1
    group = groups[0]
    member_ids = {m.market_id for m in group.members}
    assert member_ids == {"p_btc", "k_btc"}
    assert len(group.outcome_map) == 2


def test_match_excludes_hard_negative_pair():
    a, b = hard_negative_pair()
    # No shared blocking keys -> not even a candidate; definitely not grouped.
    groups = MatchingEngine().match([a, b])
    assert groups == []


def test_match_confidence_in_unit_interval():
    a, b = true_match_pair()
    groups = MatchingEngine().match([a, b])
    assert len(groups) == 1
    assert 0.0 <= groups[0].match_confidence <= 1.0


def test_match_excludes_group_when_outcomes_unmappable():
    # Strong title match but the kalshi market has non-binary outcomes, so no
    # complete outcome mapping exists -> group excluded (Req 4.6 / Property 6).
    a = market("polymarket", "p1", "Who wins the 2024 presidential election")
    b = market(
        "kalshi",
        "k1",
        "Who wins the 2024 presidential election",
        outcomes=[outcome("Democrat"), outcome("Republican")],
    )
    groups = MatchingEngine().match([a, b])
    assert groups == []


def test_match_is_deterministic_regardless_of_input_order():
    a, b = true_match_pair()
    g1 = MatchingEngine().match([a, b])
    g2 = MatchingEngine().match([b, a])
    assert [g.group_id for g in g1] == [g.group_id for g in g2]
    assert g1[0].match_confidence == g2[0].match_confidence


def test_match_groups_inverted_pair_with_alignment():
    a = market("polymarket", "p1", "Will Trump win the 2024 election")
    b = market("kalshi", "k1", "Will Trump not win the 2024 election")
    groups = MatchingEngine().match([a, b])
    assert len(groups) == 1
    canon = {al.canonical_outcome: al for al in groups[0].outcome_map}
    assert canon["YES"].inverted["kalshi"] is True


def test_match_respects_score_threshold():
    a, b = true_match_pair()
    # An impossibly high threshold excludes even strong matches.
    strict = MatchingEngine(score_threshold=0.999)
    assert strict.match([a, b]) == []


def test_match_multiple_topics_produces_multiple_groups():
    markets = [
        market("polymarket", "p_btc", "Will Bitcoin close above 100000 in 2025"),
        market("kalshi", "k_btc", "Bitcoin above 100000 by 2025"),
        market("polymarket", "p_eth", "Will Ethereum exceed 5000 in 2025"),
        market("kalshi", "k_eth", "Ethereum above 5000 during 2025"),
    ]
    groups = MatchingEngine().match(markets)
    group_member_sets = [frozenset(m.market_id for m in g.members) for g in groups]
    assert frozenset({"p_btc", "k_btc"}) in group_member_sets
    assert frozenset({"p_eth", "k_eth"}) in group_member_sets
