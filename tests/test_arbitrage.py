"""Unit tests for the ArbitrageEngine (Req 4.5, 5.1-5.6, 8.2).

Table-driven cases with hand-computed expected ``net_profit_margin`` cover
positive, zero, and negative margins, fee accounting via the Kalshi fee model,
lowest-ask selection across N members, liquidity-limited sizing (Req 5.6), and
exclusion of stale (Req 5.1, 8.2, Property 5) and low-confidence (Req 4.5,
Property 7) groups.

Validates: Property 5, Property 7, Property 8, Property 9
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest

from scanner.arbitrage import ArbitrageEngine
from scanner.fees import FlatFeeModel, KalshiFeeModel
from scanner.models import (
    CanonicalMarket,
    EquivalentMarketGroup,
    Outcome,
    OutcomeAlignment,
)

# A fixed clock so detected_at / data_age_seconds are deterministic.
NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _market(
    platform: str,
    *,
    yes_ask: float,
    no_ask: float,
    yes_liq: Optional[float] = 1000.0,
    no_liq: Optional[float] = 1000.0,
    retrieved_at: datetime = NOW,
    is_stale: bool = False,
    yes_name: str = "YES",
    no_name: str = "NO",
) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=f"{platform}-mkt",
        title=f"Will event happen? ({platform})",
        outcomes=[
            Outcome(name=yes_name, price=yes_ask, ask=yes_ask, bid=yes_ask, available_liquidity_usd=yes_liq),
            Outcome(name=no_name, price=no_ask, ask=no_ask, bid=no_ask, available_liquidity_usd=no_liq),
        ],
        volume_usd=10_000.0,
        liquidity_usd=5_000.0,
        fee_rate=0.0,
        retrieved_at=retrieved_at,
        is_stale=is_stale,
    )


def _binary_group(
    members: List[CanonicalMarket],
    *,
    confidence: float = 1.0,
    yes_names: Optional[Dict[str, str]] = None,
    no_names: Optional[Dict[str, str]] = None,
) -> EquivalentMarketGroup:
    """Build a YES/NO group whose alignment maps each platform's native names."""
    yes_names = yes_names or {m.platform: "YES" for m in members}
    no_names = no_names or {m.platform: "NO" for m in members}
    outcome_map = [
        OutcomeAlignment(
            canonical_outcome="YES",
            platform_outcomes=dict(yes_names),
            inverted={m.platform: False for m in members},
        ),
        OutcomeAlignment(
            canonical_outcome="NO",
            platform_outcomes=dict(no_names),
            inverted={m.platform: False for m in members},
        ),
    ]
    group_id = "|".join(sorted(f"{m.platform}:{m.market_id}" for m in members))
    return EquivalentMarketGroup(
        group_id=group_id,
        members=members,
        outcome_map=outcome_map,
        match_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Table-driven margin computation (Req 5.2, 5.4; Property 8)
# ---------------------------------------------------------------------------

@dataclass
class MarginCase:
    name: str
    poly_yes: float
    poly_no: float
    kalshi_yes: float
    kalshi_no: float
    expected_margin: float


# All cases use a flat 0 fee on both platforms, so margins are exact and the
# arbitrage picks the lowest ask per canonical outcome across the two members.
MARGIN_CASES = [
    # YES cheap on polymarket (0.40), NO cheap on kalshi (0.55) -> cost 0.95.
    # net_margin = (1 - 0.95) / 0.95 = 0.0526315...
    MarginCase("positive", 0.40, 0.62, 0.45, 0.55, (1 - 0.95) / 0.95),
    # cost exactly 1.0 -> margin 0 (Req 5.3 boundary).
    MarginCase("zero", 0.50, 0.55, 0.55, 0.50, 0.0),
    # cost 1.10 -> negative margin (still recorded, Req 5.3).
    MarginCase("negative", 0.60, 0.70, 0.65, 0.50, (1 - 1.10) / 1.10),
]


@pytest.mark.parametrize("case", MARGIN_CASES, ids=lambda c: c.name)
def test_net_profit_margin_flat_fees(case: MarginCase):
    poly = _market("polymarket", yes_ask=case.poly_yes, no_ask=case.poly_no)
    kalshi = _market("kalshi", yes_ask=case.kalshi_yes, no_ask=case.kalshi_no)
    group = _binary_group([poly, kalshi])

    engine = ArbitrageEngine(
        fee_models={"polymarket": FlatFeeModel(0.0), "kalshi": FlatFeeModel(0.0)},
        clock=_clock,
    )
    opp = engine.evaluate_group(group)

    assert opp is not None
    assert opp.net_profit_margin == pytest.approx(case.expected_margin)
    assert opp.detected_at == NOW
    # Two legs, one per canonical outcome, both buys.
    assert len(opp.legs) == 2
    assert {leg.side for leg in opp.legs} == {"buy"}


def test_records_opportunity_for_zero_and_negative_margins():
    # Req 5.3: an opportunity is recorded even when margin <= 0.
    poly = _market("polymarket", yes_ask=0.60, no_ask=0.70)
    kalshi = _market("kalshi", yes_ask=0.65, no_ask=0.50)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    assert opp.net_profit_margin < 0


def test_lowest_ask_chosen_per_canonical_outcome():
    # YES: poly 0.40 < kalshi 0.45 -> buy YES on polymarket.
    # NO:  kalshi 0.55 < poly 0.62 -> buy NO on kalshi.
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None

    by_outcome = {leg.outcome: leg for leg in opp.legs}
    assert by_outcome["YES"].platform == "polymarket"
    assert by_outcome["YES"].price == pytest.approx(0.40)
    assert by_outcome["NO"].platform == "kalshi"
    assert by_outcome["NO"].price == pytest.approx(0.55)


def test_generalizes_to_three_members():
    # Three platforms; lowest ask per canonical outcome wins regardless of count.
    poly = _market("polymarket", yes_ask=0.50, no_ask=0.52)
    kalshi = _market("kalshi", yes_ask=0.44, no_ask=0.58)  # cheapest YES
    manifold = _market("manifold", yes_ask=0.49, no_ask=0.49)  # cheapest NO
    group = _binary_group([poly, kalshi, manifold])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None

    by_outcome = {leg.outcome: leg for leg in opp.legs}
    assert by_outcome["YES"].platform == "kalshi"
    assert by_outcome["NO"].platform == "manifold"
    # cost = 0.44 + 0.49 = 0.93 ; margin = (1 - 0.93) / 0.93
    assert opp.net_profit_margin == pytest.approx((1 - 0.93) / 0.93)


# ---------------------------------------------------------------------------
# Fee accounting (Req 5.2; Property 8: net_margin <= gross_margin)
# ---------------------------------------------------------------------------

def test_kalshi_fee_reduces_margin():
    # YES cheap on polymarket (0.45, flat 0 fee); NO cheap on kalshi (0.50).
    poly = _market("polymarket", yes_ask=0.45, no_ask=0.60)
    kalshi = _market("kalshi", yes_ask=0.50, no_ask=0.50)
    group = _binary_group([poly, kalshi])

    engine = ArbitrageEngine(
        fee_models={"polymarket": FlatFeeModel(0.0), "kalshi": KalshiFeeModel()},
        clock=_clock,
    )
    opp = engine.evaluate_group(group)
    assert opp is not None

    # cost_per_pair = 0.45 + 0.50 = 0.95
    # kalshi fee on NO leg at 0.50 = ceil(0.07 * 0.5 * 0.5 * 100)/100 = 0.02
    # net_cost = 0.97 -> net_margin = (1 - 0.97) / 0.97
    gross_margin = (1 - 0.95) / 0.95
    net_margin = (1 - 0.97) / 0.97
    assert opp.net_profit_margin == pytest.approx(net_margin)
    # Property 8: fees never increase the margin.
    assert opp.net_profit_margin <= gross_margin


def test_property_net_margin_never_exceeds_gross_margin():
    # With any non-negative fee, net <= gross. Compare flat-0 vs kalshi fees.
    poly = _market("polymarket", yes_ask=0.45, no_ask=0.60)
    kalshi = _market("kalshi", yes_ask=0.50, no_ask=0.50)
    group = _binary_group([poly, kalshi])

    gross_engine = ArbitrageEngine(
        fee_models={"polymarket": FlatFeeModel(0.0), "kalshi": FlatFeeModel(0.0)},
        clock=_clock,
    )
    net_engine = ArbitrageEngine(
        fee_models={"polymarket": FlatFeeModel(0.0), "kalshi": KalshiFeeModel()},
        clock=_clock,
    )
    gross = gross_engine.evaluate_group(group).net_profit_margin
    net = net_engine.evaluate_group(group).net_profit_margin
    assert net <= gross


# ---------------------------------------------------------------------------
# Liquidity-bounded sizing (Req 5.6; Property 9)
# ---------------------------------------------------------------------------

def test_recommended_size_capped_by_thinnest_leg():
    # Chosen legs: YES on polymarket (liq 1000), NO on kalshi (liq 300).
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, yes_liq=1000.0, no_liq=900.0)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55, yes_liq=800.0, no_liq=300.0)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    # Thinnest chosen leg liquidity is the kalshi NO leg at 300.
    assert opp.recommended_size_usd == pytest.approx(300.0)


def test_recommended_size_never_exceeds_any_leg_liquidity():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, yes_liq=120.0, no_liq=900.0)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55, yes_liq=800.0, no_liq=450.0)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    # Chosen: YES poly (120) and NO kalshi (450) -> min 120.
    chosen_liquidities = [120.0, 450.0]
    assert opp.recommended_size_usd <= min(chosen_liquidities)


def test_unavailable_liquidity_treated_as_zero_depth():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, yes_liq=None, no_liq=900.0)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55, yes_liq=800.0, no_liq=300.0)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    # Chosen YES leg (polymarket) has unavailable liquidity -> size 0.
    assert opp.recommended_size_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Staleness exclusion (Req 5.1, 8.2; Property 5)
# ---------------------------------------------------------------------------

def test_stale_member_excludes_group():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, is_stale=True)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock)
    assert engine.evaluate_group(group) is None


def test_age_over_threshold_excludes_group():
    old = NOW - timedelta(seconds=120)
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, retrieved_at=old)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock, staleness_threshold_seconds=60.0)
    assert engine.evaluate_group(group) is None


def test_fresh_group_under_threshold_is_evaluated():
    recent = NOW - timedelta(seconds=30)
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62, retrieved_at=recent)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi])
    engine = ArbitrageEngine(clock=_clock, staleness_threshold_seconds=60.0)
    opp = engine.evaluate_group(group)
    assert opp is not None
    assert opp.data_age_seconds == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Confidence threshold exclusion (Req 4.5; Property 7)
# ---------------------------------------------------------------------------

def test_low_confidence_group_excluded():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi], confidence=0.4)
    engine = ArbitrageEngine(clock=_clock, confidence_threshold=0.6)
    assert engine.evaluate_group(group) is None


def test_confidence_at_threshold_is_evaluated():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62)
    kalshi = _market("kalshi", yes_ask=0.45, no_ask=0.55)
    group = _binary_group([poly, kalshi], confidence=0.6)
    engine = ArbitrageEngine(clock=_clock, confidence_threshold=0.6)
    assert engine.evaluate_group(group) is not None


# ---------------------------------------------------------------------------
# evaluate() over multiple groups + inverted-name handling
# ---------------------------------------------------------------------------

def test_evaluate_skips_stale_and_low_confidence_only():
    good = _binary_group(
        [
            _market("polymarket", yes_ask=0.40, no_ask=0.62),
            _market("kalshi", yes_ask=0.45, no_ask=0.55),
        ],
        confidence=0.9,
    )
    stale = _binary_group(
        [
            _market("polymarket", yes_ask=0.40, no_ask=0.62, is_stale=True),
            _market("kalshi", yes_ask=0.45, no_ask=0.55),
        ],
        confidence=0.9,
    )
    low_conf = _binary_group(
        [
            _market("polymarket", yes_ask=0.41, no_ask=0.61),
            _market("kalshi", yes_ask=0.46, no_ask=0.54),
        ],
        confidence=0.3,
    )
    engine = ArbitrageEngine(clock=_clock, confidence_threshold=0.6)
    opps = engine.evaluate([good, stale, low_conf])
    assert len(opps) == 1
    assert opps[0].group_id == good.group_id


def test_inverted_outcome_name_is_used():
    # On 'kalshi' the canonical YES maps to its native outcome literally named
    # "DOWN" (an inverted phrasing). The engine must price that outcome by name.
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.62)
    kalshi = _market(
        "kalshi", yes_ask=0.45, no_ask=0.55, yes_name="DOWN", no_name="UP"
    )
    # canonical YES -> kalshi "DOWN" (ask 0.45); canonical NO -> kalshi "UP" (0.55)
    group = _binary_group(
        [poly, kalshi],
        yes_names={"polymarket": "YES", "kalshi": "DOWN"},
        no_names={"polymarket": "NO", "kalshi": "UP"},
    )
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    # Legs carry the native outcome name. YES is cheapest on polymarket (0.40);
    # canonical NO is cheapest on kalshi, whose native name is "UP" (0.55).
    yes_leg = next(leg for leg in opp.legs if leg.platform == "polymarket")
    no_leg = next(leg for leg in opp.legs if leg.platform == "kalshi")
    assert yes_leg.outcome == "YES"
    assert yes_leg.price == pytest.approx(0.40)
    assert no_leg.outcome == "UP"
    assert no_leg.price == pytest.approx(0.55)



# ---------------------------------------------------------------------------
# ask≈0 数据缺陷不应产生虚假套利（修复 #9）
# ---------------------------------------------------------------------------

def test_zero_ask_leg_is_unpriceable_no_false_arbitrage():
    # 单平台组，YES ask=0（脏数据/无报价）：YES 不可定价 → 不产生机会，
    # 避免 (1-≈0)/≈0 的虚假超高利润率。
    poly = _market("polymarket", yes_ask=0.0, no_ask=0.40)
    group = _binary_group([poly])
    engine = ArbitrageEngine(clock=_clock)
    assert engine.evaluate_group(group) is None


def test_zero_ask_member_skipped_other_member_priced():
    # 两平台组，A 平台 YES ask=0（脏数据）应被跳过，用 B 平台真实 ask 定价。
    a = _market("polymarket", yes_ask=0.0, no_ask=0.55)
    b = _market("predictfun", yes_ask=0.50, no_ask=0.55)
    group = _binary_group([a, b])
    engine = ArbitrageEngine(clock=_clock)
    opp = engine.evaluate_group(group)
    assert opp is not None
    yes_leg = next(leg for leg in opp.legs if leg.outcome == "YES")
    # YES 取 B 平台真实 0.50，而非 A 的脏 0.0。
    assert yes_leg.platform == "predictfun"
    assert yes_leg.price == pytest.approx(0.50)



# ---------------------------------------------------------------------------
# 套利配置闸门：隐含概率背离 + 最小可成交规模（真实数据驱动，防虚假套利）
# ---------------------------------------------------------------------------

def test_divergence_gate_rejects_large_implied_prob_gap():
    # 同一事件两平台隐含 P(YES) 背离大（如 Fed cuts 0.4% vs 49%）→ 跳过。
    poly = _market("polymarket", yes_ask=0.004, no_ask=0.997)   # P(YES)≈0.4%
    pf = _market("predictfun", yes_ask=0.49, no_ask=0.51)        # P(YES)=49%
    group = _binary_group([poly, pf])
    eng = ArbitrageEngine(clock=_clock, max_implied_prob_divergence=0.10)
    assert eng.evaluate_group(group) is None


def test_divergence_gate_allows_close_probs():
    # 隐含概率接近（57% vs 55%）→ 真套利，应保留。
    poly = _market("polymarket", yes_ask=0.58, no_ask=0.43)     # P(YES)=58%
    kalshi = _market("kalshi", yes_ask=0.55, no_ask=0.46)       # P(YES)=55%
    group = _binary_group([poly, kalshi])
    eng = ArbitrageEngine(clock=_clock, max_implied_prob_divergence=0.10)
    opp = eng.evaluate_group(group)
    assert opp is not None
    assert opp.net_profit_margin > 0  # 买 kalshi YES 0.55 + poly NO 0.43 = 0.98


def test_divergence_gate_disabled_by_default():
    # 默认不启用（None）：背离大也照常评估（保持向后兼容）。
    poly = _market("polymarket", yes_ask=0.004, no_ask=0.997)
    pf = _market("predictfun", yes_ask=0.49, no_ask=0.51)
    group = _binary_group([poly, pf])
    eng = ArbitrageEngine(clock=_clock)  # 无背离闸门
    assert eng.evaluate_group(group) is not None


def test_min_size_gate_rejects_thin_liquidity():
    # 可成交规模受最薄腿流动性限制；低于下限 → 跳过（gas 会吃掉利润）。
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.55, yes_liq=65.0, no_liq=65.0)
    kalshi = _market("kalshi", yes_ask=0.42, no_ask=0.55, yes_liq=65.0, no_liq=65.0)
    group = _binary_group([poly, kalshi])
    eng = ArbitrageEngine(clock=_clock, min_recommended_size_usd=100.0)
    assert eng.evaluate_group(group) is None


def test_min_size_gate_allows_sufficient_liquidity():
    poly = _market("polymarket", yes_ask=0.40, no_ask=0.55, yes_liq=5000.0, no_liq=5000.0)
    kalshi = _market("kalshi", yes_ask=0.42, no_ask=0.55, yes_liq=5000.0, no_liq=5000.0)
    group = _binary_group([poly, kalshi])
    eng = ArbitrageEngine(clock=_clock, min_recommended_size_usd=100.0)
    assert eng.evaluate_group(group) is not None



# ---------------------------------------------------------------------------
# 市场自报费率纳入净利润率（修复：predict.fun feeRateBps 此前被忽略）
# ---------------------------------------------------------------------------

def _market_with_fee(platform, *, yes_ask, no_ask, fee_rate):
    m = _market(platform, yes_ask=yes_ask, no_ask=no_ask)
    return m.model_copy(update={"fee_rate": fee_rate})


def test_market_fee_rate_included_in_margin():
    # 复刻「外星人」真实案例：毛利 ~2.1%，但 predict.fun 2% 费应被扣掉 → 净≈0.36%。
    poly = _market_with_fee("polymarket", yes_ask=0.11, no_ask=0.90, fee_rate=0.0)
    pf = _market_with_fee("predictfun", yes_ask=0.136, no_ask=0.869, fee_rate=0.02)
    group = _binary_group([poly, pf])
    eng = ArbitrageEngine(clock=_clock)  # 配置无 predictfun 费用模型（默认 0）
    opp = eng.evaluate_group(group)
    assert opp is not None
    # 成本 = poly YES 0.11 + pf NO 0.869 = 0.979；pf 费 = 0.02×0.869 = 0.01738。
    # 净成本 0.99638 → 净利润率 ≈ 0.363%（而非未扣费的 2.14%）。
    assert opp.net_profit_margin == pytest.approx((1-0.99638)/0.99638, abs=1e-4)
    assert opp.net_profit_margin < 0.01  # 远低于未扣费的 2.1%


def test_market_fee_does_not_lower_below_config_model():
    # 取较大值：配置模型费用更高时仍用模型（保留 Kalshi 非线性）。
    poly = _market_with_fee("polymarket", yes_ask=0.40, no_ask=0.62, fee_rate=0.0)
    kalshi = _market_with_fee("kalshi", yes_ask=0.45, no_ask=0.55, fee_rate=0.0)
    group = _binary_group([poly, kalshi])
    eng = ArbitrageEngine(fee_models={"kalshi": KalshiFeeModel()}, clock=_clock)
    opp = eng.evaluate_group(group)
    assert opp is not None  # Kalshi 非线性费用仍生效（市场 fee_rate=0 不会把它降为 0）
