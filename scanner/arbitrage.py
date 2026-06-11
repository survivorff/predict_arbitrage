"""Cross-platform arbitrage detection (Req 5.1-5.6, 8.2).

The :class:`ArbitrageEngine` consumes :class:`EquivalentMarketGroup` objects
from the MatchingEngine and, for every group that is fresh and meets the
configured match-confidence threshold, computes a net-of-fees arbitrage margin
and records an :class:`ArbitrageOpportunity`.

The canonical arbitrage for a binary market matched across platforms is to buy
the *cheapest* contract for each canonical outcome (e.g. YES on the platform
where YES is cheap, NO on the platform where NO is cheap). If the summed cost of
assembling one contract per canonical outcome is below the $1 guaranteed payout
(after fees), the difference is locked-in profit::

    cost_per_pair = sum over canonical outcomes of (lowest ask across members)
    gross_margin  = (1 - cost_per_pair) / cost_per_pair
    fees_per_pair = sum over chosen legs of feeModel[platform].fee_for(ask, 1)
    net_cost      = cost_per_pair + fees_per_pair
    net_margin    = (1 - net_cost) / net_cost            # Req 5.2
    recommended_size_usd capped by the thinnest chosen leg's liquidity  # Req 5.6

Design choices wired to requirements / correctness properties:

- Uses **ask** prices (cost to cross the spread), falling back to ``price`` when
  an ask is unavailable (Req 5.2).
- Evaluates only non-stale groups (Req 5.1, 8.2, Property 5).
- Evaluates only groups meeting the match-confidence threshold (Req 4.5,
  Property 7).
- Records an opportunity for every evaluated group, *including* margins <= 0
  (Req 5.3); the OpportunityStore is responsible for filtering/removal (task 11).
- ``net_margin <= gross_margin`` always, since fees are non-negative
  (Property 8).
- ``recommended_size_usd`` never exceeds the thinnest leg's liquidity
  (Property 9).
- Generalizes to N members: per canonical outcome the platform offering the
  lowest ask is chosen.

A clock is injected so ``detected_at`` and ``data_age_seconds`` are deterministic
in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from scanner.fees import FeeModel, FlatFeeModel
from scanner.models import (
    ArbitrageOpportunity,
    ArbLeg,
    CanonicalMarket,
    EquivalentMarketGroup,
    Outcome,
    OutcomeAlignment,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# 最小可成交 ask：低于等于此值的 ask 视为数据缺陷（价格≈0 处不存在真实卖单，
# 通常是空/脏订单簿档位），不可定价。防止 (1-≈0)/≈0 这类虚假超高利润率。
MIN_TRADEABLE_ASK = 0.001


@dataclass(frozen=True)
class _ChosenLeg:
    """The cheapest member for one canonical outcome, with sizing inputs."""

    platform: str
    market_id: str
    outcome_name: str
    ask: float
    liquidity_usd: Optional[float]
    age_seconds: float
    fee_rate: Optional[float] = None  # 市场自报的费率（如 predict.fun feeRateBps→0.02）


@dataclass
class ArbitrageEngine:
    """Detects cross-platform arbitrage opportunities net of fees and spread.

    Args:
        fee_models: Per-platform fee models. The fee for a chosen leg is computed
            from ``fee_models[platform]``; platforms missing from the map fall
            back to ``default_fee_model``.
        confidence_threshold: Groups with ``match_confidence`` below this value
            are skipped (Req 4.5, Property 7).
        default_fee_model: Fee model used for any platform absent from
            ``fee_models``. Defaults to a zero-rate flat model.
        clock: Callable returning the current UTC time, injected for
            deterministic ``detected_at`` / ``data_age_seconds`` in tests.
        staleness_threshold_seconds: Optional age cap. When set, a member whose
            data age exceeds this threshold marks its group stale (in addition to
            the member's own ``is_stale`` flag), so the group is skipped
            (Property 5). When ``None`` the engine relies solely on ``is_stale``.
    """

    fee_models: Dict[str, FeeModel] = field(default_factory=dict)
    confidence_threshold: float = 0.0
    default_fee_model: FeeModel = field(default_factory=FlatFeeModel)
    clock: Callable[[], datetime] = _utc_now
    staleness_threshold_seconds: Optional[float] = None
    # 套利配置（数据可靠性 / 可成交性闸门，真实数据驱动）。
    # max_implied_prob_divergence：同一事件两平台隐含 P(YES) 背离超过此值（绝对概率差）
    #   则跳过——大背离意味着一侧定价脏/薄/结算口径不同，而非真套利（如 Fed cuts 0.4% vs
    #   17.6%）。None=不启用。
    # min_recommended_size_usd：可成交规模（受最薄腿流动性限制）低于此值则跳过——
    #   薄盘机会的利润易被链上 gas 吃掉。0=不启用。
    max_implied_prob_divergence: Optional[float] = None
    min_recommended_size_usd: float = 0.0

    # -- public API ---------------------------------------------------------

    def evaluate(
        self, groups: List[EquivalentMarketGroup]
    ) -> List[ArbitrageOpportunity]:
        """Evaluate every eligible group and return recorded opportunities.

        Stale groups (Req 5.1, 8.2, Property 5) and sub-threshold-confidence
        groups (Req 4.5, Property 7) are skipped. Every remaining group that can
        be priced produces an opportunity, including margins <= 0 (Req 5.3).
        """
        opportunities: List[ArbitrageOpportunity] = []
        for group in groups:
            opportunity = self.evaluate_group(group)
            if opportunity is not None:
                opportunities.append(opportunity)
        return opportunities

    def evaluate_group(
        self, group: EquivalentMarketGroup
    ) -> Optional[ArbitrageOpportunity]:
        """Evaluate one group; return its opportunity or ``None`` if skipped.

        Returns ``None`` when the group is stale, below the confidence
        threshold, or cannot be priced (a canonical outcome has no member with a
        usable price, or the assembled cost is non-positive).
        """
        # Req 4.5 / Property 7: groups below the confidence threshold never reach
        # detection.
        if group.match_confidence < self.confidence_threshold:
            return None

        # Req 5.1 / 8.2 / Property 5: never derive an opportunity from a group
        # containing stale data.
        if self._group_is_stale(group):
            return None

        # 数据可靠性闸门：同一事件两平台隐含 P(YES) 背离过大 → 跳过（脏/薄/口径不同，
        # 非真套利）。在选腿/定价之前判断，避免产出虚假机会。
        if self.max_implied_prob_divergence is not None:
            if self._implied_prob_divergence(group) > self.max_implied_prob_divergence:
                return None

        # Choose the cheapest member per canonical outcome.
        chosen: List[_ChosenLeg] = []
        for alignment in group.outcome_map:
            leg = self._cheapest_leg(group.members, alignment)
            if leg is None:
                # A canonical outcome cannot be priced on any member; the group
                # cannot be assembled into an arbitrage.
                return None
            chosen.append(leg)

        if not chosen:
            return None

        cost_per_pair = sum(leg.ask for leg in chosen)
        if cost_per_pair <= 0:
            # Degenerate pricing (e.g. all asks zero); margin is undefined.
            return None

        fees_per_pair = sum(self._leg_fee(leg) for leg in chosen)
        net_cost = cost_per_pair + fees_per_pair

        # Req 5.2 / Property 8: margin computed from ask prices net of fees.
        net_margin = (1.0 - net_cost) / net_cost

        # Req 5.6 / Property 9: size never exceeds the thinnest leg's liquidity.
        recommended_size_usd = self._recommended_size(chosen)

        # 可成交性闸门：规模过小（薄盘）→ 利润易被 gas 吃掉，跳过。
        if recommended_size_usd < self.min_recommended_size_usd:
            return None

        now = self.clock()
        data_age_seconds = max(leg.age_seconds for leg in chosen)

        legs = [
            ArbLeg(
                platform=leg.platform,
                market_id=leg.market_id,
                outcome=leg.outcome_name,
                side="buy",
                price=leg.ask,
            )
            for leg in chosen
        ]

        return ArbitrageOpportunity(
            group_id=group.group_id,
            event_title=self._event_title(group),
            legs=legs,
            net_profit_margin=net_margin,
            recommended_size_usd=recommended_size_usd,
            detected_at=now,
            data_age_seconds=data_age_seconds,
        )

    # -- helpers ------------------------------------------------------------

    def _fee_model_for(self, platform: str) -> FeeModel:
        return self.fee_models.get(platform, self.default_fee_model)

    def _leg_fee(self, leg: "_ChosenLeg") -> float:
        """一条腿的手续费，取「配置费用模型」与「市场自报费率」的**较大值**。

        修复：此前只用配置 ``fee_models``（predict.fun 在 config 里是 flat 0%），完全
        忽略了适配器从 ``feeRateBps`` 读到的真实费率（如 predict.fun 2%），导致净利润率
        被高估——真实数据中「外星人」市场 2.1% 的"套利"扣掉 predict.fun 2% 费后≈0.36%。
        取较大值：保留 Kalshi 非线性模型，又用上市场真实费率，**永不低估费用、不高估利润**。
        """
        model_fee = self._fee_model_for(leg.platform).fee_for(leg.ask, 1.0)
        market_fee = (leg.fee_rate * leg.ask) if leg.fee_rate is not None else 0.0
        return max(model_fee, market_fee)

    def _implied_prob_divergence(self, group: EquivalentMarketGroup) -> float:
        """组内各成员隐含 P(YES) 的最大背离（绝对概率差）。

        同一事件两平台的隐含概率本应接近；背离大是「定价脏/薄/结算口径不同」的强信号，
        而非真套利。取每个成员 YES 结果的 price（隐含概率）；无 YES 结果的成员忽略。
        成员不足 2 个可比价时返回 0（不触发过滤）。
        """
        probs: List[float] = []
        for member in group.members:
            yes = self._find_outcome_by_polarity(member, affirmative=True)
            if yes is not None:
                probs.append(yes.price)
        if len(probs) < 2:
            return 0.0
        return max(probs) - min(probs)

    @staticmethod
    def _find_outcome_by_polarity(
        market: CanonicalMarket, *, affirmative: bool
    ) -> Optional[Outcome]:
        """返回市场中 YES（affirmative=True）或 NO 极性的结果。"""
        target = "YES" if affirmative else "NO"
        for outcome in market.outcomes:
            if outcome.name.strip().upper() == target:
                return outcome
        return None

    def _group_is_stale(self, group: EquivalentMarketGroup) -> bool:
        """True when any member is stale (Req 8.2, Property 5)."""
        for member in group.members:
            if member.is_stale:
                return True
            if self.staleness_threshold_seconds is not None:
                if self._age_seconds(member) > self.staleness_threshold_seconds:
                    return True
        return False

    def _cheapest_leg(
        self, members: List[CanonicalMarket], alignment: OutcomeAlignment
    ) -> Optional[_ChosenLeg]:
        """Pick the member offering the lowest ask for a canonical outcome.

        Uses the alignment's per-platform native outcome name (which already
        accounts for ``inverted`` phrasing). The ask price is the cost to cross
        the spread; when an ask is unavailable the implied-probability ``price``
        is used as a sensible fallback.
        """
        best: Optional[_ChosenLeg] = None
        for member in members:
            native_name = alignment.platform_outcomes.get(member.platform)
            if native_name is None:
                continue
            outcome = self._find_outcome(member, native_name)
            if outcome is None:
                continue
            ask = self._effective_ask(outcome)
            if ask is None:
                continue
            candidate = _ChosenLeg(
                platform=member.platform,
                market_id=member.market_id,
                outcome_name=native_name,
                ask=ask,
                liquidity_usd=outcome.available_liquidity_usd,
                age_seconds=self._age_seconds(member),
                fee_rate=member.fee_rate,
            )
            if best is None or self._is_cheaper(candidate, best):
                best = candidate
        return best

    @staticmethod
    def _is_cheaper(candidate: _ChosenLeg, current: _ChosenLeg) -> bool:
        """Cheaper ask wins; ties broken deterministically by platform/market."""
        if candidate.ask != current.ask:
            return candidate.ask < current.ask
        return (candidate.platform, candidate.market_id) < (
            current.platform,
            current.market_id,
        )

    @staticmethod
    def _find_outcome(
        market: CanonicalMarket, name: str
    ) -> Optional[Outcome]:
        for outcome in market.outcomes:
            if outcome.name == name:
                return outcome
        return None

    @staticmethod
    def _effective_ask(outcome: Outcome) -> Optional[float]:
        """Ask price for crossing the spread, falling back to ``price``.

        Returns ``None`` when no usable, *tradeable* ask exists. An ask at or
        below ``MIN_TRADEABLE_ASK`` is treated as a data artifact (no real offer
        is made at price ~0; it usually means an empty/garbage book level) rather
        than a free winning contract — pricing off it would fabricate an
        enormous fake margin ``(1 - ~0) / ~0``. Such legs are unpriceable.
        """
        ask = outcome.ask if outcome.ask is not None else outcome.price
        if ask is None or ask <= MIN_TRADEABLE_ASK:
            return None
        return ask

    @staticmethod
    def _recommended_size(chosen: List[_ChosenLeg]) -> float:
        """Cap size by the thinnest leg's liquidity (Req 5.6, Property 9).

        A leg whose liquidity is unavailable is treated as zero depth, so the
        engine never recommends a size it cannot justify against known liquidity.
        """
        depths = [
            leg.liquidity_usd if leg.liquidity_usd is not None else 0.0
            for leg in chosen
        ]
        if not depths:
            return 0.0
        return min(depths)

    def _age_seconds(self, market: CanonicalMarket) -> float:
        """Data age in seconds computed against the injected clock."""
        retrieved = market.retrieved_at
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        return (self.clock() - retrieved).total_seconds()

    @staticmethod
    def _event_title(group: EquivalentMarketGroup) -> str:
        """Best-effort event title from the group's members."""
        for member in group.members:
            if member.title:
                return member.title
        return group.group_id


__all__ = ["ArbitrageEngine"]
