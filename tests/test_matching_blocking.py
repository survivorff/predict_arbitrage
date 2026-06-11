"""Unit tests for blocking and the SemanticSimilarity lexical default (Req 4.1).

Covers the first tier of the MatchingEngine: candidate generation via blocking
and the lexical token-set similarity that the scoring tier (task 9.2) builds on.

**Validates: Requirements 4.1**
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.matching import (
    Blocker,
    CandidatePair,
    LexicalSimilarity,
    SemanticSimilarity,
    all_cross_platform_pairs,
    extract_entities,
    normalize_text,
    tokenize,
)
from scanner.models import CanonicalMarket

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def market(platform: str, market_id: str, title: str) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=[],
        retrieved_at=NOW,
    )


# --- text helpers -----------------------------------------------------------


def test_normalize_text_lowercases_and_strips_punctuation():
    assert normalize_text("Will BITCOIN close >$100,000?") == "will bitcoin close 100 000"


def test_tokenize_splits_on_non_alphanumeric():
    assert tokenize("Trump vs. Biden 2024!") == ["trump", "vs", "biden", "2024"]


def test_extract_entities_keeps_numbers_and_significant_words():
    entities = extract_entities("Will Bitcoin close above $4000 in 2025?")
    assert "bitcoin" in entities
    assert "4000" in entities
    assert "2025" in entities
    # stopwords and short words dropped
    assert "in" not in entities
    assert "the" not in entities


def test_extract_entities_order_independent():
    a = extract_entities("Bitcoin above 4000 by 2025")
    b = extract_entities("By 2025 will Bitcoin be above 4000")
    assert {"bitcoin", "4000", "2025"}.issubset(a)
    assert {"bitcoin", "4000", "2025"}.issubset(b)


# --- LexicalSimilarity ------------------------------------------------------


def test_lexical_similarity_satisfies_protocol():
    assert isinstance(LexicalSimilarity(), SemanticSimilarity)


def test_lexical_similarity_identical_titles_score_one():
    sim = LexicalSimilarity()
    assert sim.score("Bitcoin above 100k in 2025", "Bitcoin above 100k in 2025") == 1.0


def test_lexical_similarity_is_word_order_invariant():
    sim = LexicalSimilarity()
    # Same token set, different order -> token-set ratio is 1.0.
    assert sim.score("Trump wins 2024 election", "2024 election Trump wins") == 1.0


def test_lexical_similarity_robust_to_extra_qualifiers():
    sim = LexicalSimilarity()
    score = sim.score(
        "Will Bitcoin close above 100000 in 2025",
        "Bitcoin above 100000 by end of 2025",
    )
    assert score >= 0.6


def test_lexical_similarity_scores_known_match_above_known_nonmatch():
    sim = LexicalSimilarity()
    match = sim.score(
        "Will the Lakers win the 2025 NBA championship",
        "Lakers to win 2025 NBA championship",
    )
    nonmatch = sim.score(
        "Will the Lakers win the 2025 NBA championship",
        "Will it rain in Seattle tomorrow",
    )
    assert match > nonmatch
    assert match >= 0.7
    assert nonmatch <= 0.3


def test_lexical_similarity_unrelated_titles_score_low():
    sim = LexicalSimilarity()
    assert sim.score("Bitcoin price prediction", "Presidential election winner") < 0.3


def test_lexical_similarity_empty_against_nonempty_is_zero():
    sim = LexicalSimilarity()
    assert sim.score("", "Bitcoin above 100k") == 0.0


def test_lexical_similarity_both_empty_is_one():
    sim = LexicalSimilarity()
    assert sim.score("", "") == 1.0


def test_lexical_similarity_is_symmetric():
    sim = LexicalSimilarity()
    a, b = "Eagles win Super Bowl 2025", "2025 Super Bowl Eagles victory"
    assert sim.score(a, b) == sim.score(b, a)


# --- Blocker ----------------------------------------------------------------


def test_blocker_pairs_only_cross_platform_matches():
    markets = [
        market("polymarket", "p1", "Will Bitcoin close above 100000 in 2025"),
        market("kalshi", "k1", "Bitcoin above 100000 by 2025"),
    ]
    pairs = Blocker().candidate_pairs(markets)
    assert len(pairs) == 1
    pair = pairs[0]
    platforms = {pair.a.platform, pair.b.platform}
    assert platforms == {"polymarket", "kalshi"}


def test_blocker_excludes_same_platform_pairs():
    markets = [
        market("polymarket", "p1", "Bitcoin above 100000 in 2025"),
        market("polymarket", "p2", "Bitcoin above 100000 in 2025"),
    ]
    # Same shared entities but identical platform -> no candidate pairs (Req 4.1).
    assert Blocker().candidate_pairs(markets) == []


def test_blocker_excludes_unrelated_markets():
    markets = [
        market("polymarket", "p1", "Will Bitcoin close above 100000 in 2025"),
        market("kalshi", "k1", "Will the Lakers win the championship"),
    ]
    # No shared blocking keys -> not a candidate pair.
    assert Blocker().candidate_pairs(markets) == []


def test_blocking_reduces_candidate_pairs_vs_full_cross_product():
    # Two genuinely-related cross-platform topics plus unrelated noise.
    markets = [
        market("polymarket", "p_btc", "Will Bitcoin close above 100000 in 2025"),
        market("kalshi", "k_btc", "Bitcoin above 100000 by 2025"),
        market("polymarket", "p_eth", "Ethereum above 5000 in 2025"),
        market("kalshi", "k_eth", "Will Ethereum exceed 5000 during 2025"),
        market("polymarket", "p_weather", "Will it rain in Seattle tomorrow"),
        market("kalshi", "k_election", "Republican nominee for president"),
    ]
    baseline = all_cross_platform_pairs(markets)
    blocked = Blocker().candidate_pairs(markets)

    # Blocking must strictly reduce the candidate space here.
    assert len(blocked) < len(baseline)

    # And it must retain the two true cross-platform pairs.
    blocked_keys = {p.key for p in blocked}
    btc_pair = CandidatePair(
        a=markets[0] if markets[0].market_id <= markets[1].market_id else markets[1],
        b=markets[1] if markets[0].market_id <= markets[1].market_id else markets[0],
    )
    assert any("p_btc" in k[0] or "p_btc" in k[1] for k in blocked_keys)
    assert any("p_eth" in k[0] or "p_eth" in k[1] for k in blocked_keys)


def test_blocker_dedupes_pairs_sharing_multiple_keys():
    # These share two entity tokens ("bitcoin", "100000"); the pair must appear once.
    markets = [
        market("polymarket", "p1", "Bitcoin 100000 2025"),
        market("kalshi", "k1", "Bitcoin 100000 2025"),
    ]
    pairs = Blocker().candidate_pairs(markets)
    assert len(pairs) == 1


def test_blocker_pairs_are_deterministically_ordered():
    markets = [
        market("kalshi", "k1", "Bitcoin above 100000 in 2025"),
        market("polymarket", "p1", "Bitcoin above 100000 in 2025"),
    ]
    pairs1 = Blocker().candidate_pairs(markets)
    pairs2 = Blocker().candidate_pairs(list(reversed(markets)))
    assert [p.key for p in pairs1] == [p.key for p in pairs2]
    # Canonical ordering: (platform, market_id) ascending.
    assert pairs1[0].a.platform == "kalshi"
    assert pairs1[0].b.platform == "polymarket"


def test_blocker_uses_extra_extractors_for_category_blocking():
    # Titles share no entity tokens, but a supplied category extractor blocks them.
    m1 = market("polymarket", "p1", "Republican primary frontrunner")
    m2 = market("kalshi", "k1", "GOP nomination leader")

    def category(_m: CanonicalMarket):
        return ["category:us-politics"]

    no_extra = Blocker().candidate_pairs([m1, m2])
    with_extra = Blocker(extra_extractors=[category]).candidate_pairs([m1, m2])

    assert no_extra == []
    assert len(with_extra) == 1


def test_blocker_keys_for_includes_entities_and_extra_keys():
    m = market("polymarket", "p1", "Bitcoin above 100000")

    def close_date(_m: CanonicalMarket):
        return ["close:2025-12-31"]

    keys = Blocker(extra_extractors=[close_date]).keys_for(m)
    assert "entity:bitcoin" in keys
    assert "entity:100000" in keys
    assert "close:2025-12-31" in keys


def test_blocker_market_with_no_keys_produces_no_pairs():
    # Title reduces to only stopwords/short tokens -> no entity keys.
    markets = [
        market("polymarket", "p1", "to be or not"),
        market("kalshi", "k1", "to be or not"),
    ]
    assert Blocker().candidate_pairs(markets) == []
