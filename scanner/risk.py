"""风控:下单前的最后闸门(Phase Three · 切片 G)。

真实资金安全是最高原则。任何能下单的代码,bug 即亏损。本模块在「检测到机会」与
「真正下单」之间插入一道**确定、可测、可解释**的风控闸门 :class:`RiskManager`,
对每个候选机会逐条校验,并给出**通过/拦截**决策与**核准规模**。

风控规则(全部可配置阈值,逐条独立校验,任一拦截即整体拦截):
1. **最小净利润率**:机会净利润率必须 ≥ ``min_net_profit_margin``(扣费后仍有套利空间)。
2. **信号新鲜度**:机会数据年龄必须 ≤ ``max_data_age_seconds``(过期价格不交易)。
3. **滑点保护**:若提供实时观测价,各腿实际可成交价偏离信号价不得超过 ``max_slippage``
   (价格已跑掉则放弃,避免按陈旧价下单)。
4. **单笔规模上限**:核准规模 ≤ ``max_trade_size_usd``。
5. **单市场敞口上限**:核准规模 + 该市场已有敞口 ≤ ``max_market_exposure_usd``。
6. **总敞口上限**:核准规模 + 当前总敞口 ≤ ``max_total_exposure_usd``。
7. **余额充足**:核准规模 ≤ 可用余额(若提供)。

核准规模取「机会建议规模」与上述各上限留出的余量的最小值;若任一上限已无余量
(核准规模 ≤ 0),则整体拦截。

**dry-run(默认开启)**:决策携带 ``dry_run`` 标志。dry-run 下走完整风控与执行决策,
但只记录「将要下的单」而不真正提交,作为生产前验证。是否需人工确认由 ``require_confirmation``
表达(供切片 H 的确认流消费)。

设计取向:RiskManager 是纯函数式的闸门——给定机会与当前敞口/余额/观测价快照,
返回一个可序列化、可解释的 :class:`RiskDecision`(含每条检查的通过与否)。不持有状态、
不触达平台,因此完全确定可测。敞口/余额由调用方(切片 H 编排层)提供。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from scanner.models import ArbitrageOpportunity
from scanner.sizing import bankroll_cap_usd


# 滑点观测价的键:(platform, market_id, outcome) -> 实际可成交价。
PriceKey = Tuple[str, str, str]


@dataclass(frozen=True)
class RiskLimits:
    """风控阈值配置(全部带稳妥默认值)。

    默认值偏保守(小额、低敞口、要求人工确认、dry-run 开启),适合生产前验证;
    运营者按风险偏好放宽。
    """

    max_trade_size_usd: float = 100.0          # 单笔最大投入
    max_market_exposure_usd: float = 200.0     # 单市场最大敞口
    max_total_exposure_usd: float = 1000.0     # 总敞口上限
    max_slippage: float = 0.02                 # 允许的价格偏离(绝对值,概率单位)
    min_net_profit_margin: float = 0.02        # 最小净利润率门槛
    max_data_age_seconds: float = 30.0         # 信号最大数据年龄
    # bankroll 分数上限（¼-Kelly 风格，P0）：单笔最多投入 bankroll 的比例，防止单笔
    # 「看似稳赚」出错造成重大回撤。bankroll_usd<=0 表示不启用（默认，行为不变）。
    bankroll_usd: float = 0.0
    max_bankroll_fraction: float = 0.25
    dry_run: bool = True                       # 默认 dry-run(不真正下单)
    require_confirmation: bool = True          # 默认需人工确认

    def __post_init__(self) -> None:
        for name in (
            "max_trade_size_usd",
            "max_market_exposure_usd",
            "max_total_exposure_usd",
            "max_slippage",
            "max_data_age_seconds",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 必须 >= 0")


@dataclass(frozen=True)
class RiskCheck:
    """单条风控检查的结果(可解释)。"""

    name: str
    passed: bool
    detail: str


@dataclass
class RiskDecision:
    """一次风控评估的决策结果。"""

    approved: bool
    checks: List[RiskCheck] = field(default_factory=list)
    approved_size_usd: float = 0.0
    dry_run: bool = True
    require_confirmation: bool = True

    @property
    def rejected_reasons(self) -> List[str]:
        """未通过的检查明细(供告警/日志/UI 展示)。"""
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def to_dict(self) -> Dict[str, object]:
        """可 JSON 序列化的快照(供 API/日志)。"""
        return {
            "approved": self.approved,
            "approved_size_usd": self.approved_size_usd,
            "dry_run": self.dry_run,
            "require_confirmation": self.require_confirmation,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


class RiskManager:
    """下单前的最后闸门(Phase Three · 切片 G)。

    Args:
        limits: 风控阈值;省略则用稳妥默认值。
    """

    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        opportunity: ArbitrageOpportunity,
        *,
        current_total_exposure_usd: float = 0.0,
        market_exposure_usd: float = 0.0,
        available_balance_usd: Optional[float] = None,
        observed_prices: Optional[Dict[PriceKey, float]] = None,
    ) -> RiskDecision:
        """对一个候选机会做完整风控评估,返回通过/拦截决策与核准规模。

        Args:
            opportunity: 候选套利机会。
            current_total_exposure_usd: 当前已占用的总敞口(USD)。
            market_exposure_usd: 该机会所涉市场上已有的敞口(USD)。
            available_balance_usd: 可用余额(USD);None 表示不校验余额。
            observed_prices: 实时观测的各腿可成交价(滑点保护用);None 表示跳过滑点检查。

        Returns:
            一个 :class:`RiskDecision`,``approved`` 为 True 当且仅当所有检查通过
            且核准规模 > 0。
        """
        limits = self.limits
        checks: List[RiskCheck] = []

        # 规则 1:最小净利润率。
        margin_ok = opportunity.net_profit_margin >= limits.min_net_profit_margin
        checks.append(
            RiskCheck(
                "min_net_profit_margin",
                margin_ok,
                f"净利润率 {opportunity.net_profit_margin:.4f} "
                f"{'≥' if margin_ok else '<'} 门槛 {limits.min_net_profit_margin:.4f}",
            )
        )

        # 规则 2:信号新鲜度。
        fresh_ok = opportunity.data_age_seconds <= limits.max_data_age_seconds
        checks.append(
            RiskCheck(
                "freshness",
                fresh_ok,
                f"数据年龄 {opportunity.data_age_seconds:.1f}s "
                f"{'≤' if fresh_ok else '>'} 上限 {limits.max_data_age_seconds:.1f}s",
            )
        )

        # 规则 3:滑点保护(仅当提供观测价时校验)。
        slippage_ok = True
        if observed_prices is not None:
            worst = 0.0
            worst_leg = ""
            for leg in opportunity.legs:
                key = (leg.platform, leg.market_id, leg.outcome)
                observed = observed_prices.get(key)
                if observed is None:
                    continue
                dev = abs(observed - leg.price)
                if dev > worst:
                    worst, worst_leg = dev, f"{leg.platform}:{leg.market_id}:{leg.outcome}"
            slippage_ok = worst <= limits.max_slippage
            checks.append(
                RiskCheck(
                    "slippage",
                    slippage_ok,
                    f"最大滑点 {worst:.4f}"
                    + (f"（{worst_leg}）" if worst_leg else "")
                    + f" {'≤' if slippage_ok else '>'} 上限 {limits.max_slippage:.4f}",
                )
            )

        # 规模上限:取各约束留出余量的最小值（不含余额——余额作为独立的充足性检查，
        # 不足时直接拦截而非悄悄缩小规模，风控语义更清晰）。
        remaining_market = limits.max_market_exposure_usd - market_exposure_usd
        remaining_total = limits.max_total_exposure_usd - current_total_exposure_usd
        size_caps = [
            opportunity.recommended_size_usd,
            limits.max_trade_size_usd,
            remaining_market,
            remaining_total,
        ]
        # bankroll 分数上限（¼-Kelly 风格）：单笔不超过 bankroll×fraction（启用时）。
        bcap = bankroll_cap_usd(limits.bankroll_usd, limits.max_bankroll_fraction)
        if bcap != float("inf"):
            size_caps.append(bcap)
        approved_size = min(size_caps)
        approved_size = max(0.0, approved_size)

        # 规则 4:单笔规模上限(核准规模受单笔上限约束;若被压到 0 则拦截)。
        trade_ok = approved_size > 0 and limits.max_trade_size_usd > 0
        checks.append(
            RiskCheck(
                "max_trade_size",
                trade_ok,
                f"核准规模 ${approved_size:.2f}（单笔上限 ${limits.max_trade_size_usd:.2f}）",
            )
        )

        # 规则 5:单市场敞口上限。
        market_ok = remaining_market > 0
        checks.append(
            RiskCheck(
                "market_exposure",
                market_ok,
                f"市场剩余敞口 ${remaining_market:.2f}"
                f"（已用 ${market_exposure_usd:.2f} / 上限 ${limits.max_market_exposure_usd:.2f}）",
            )
        )

        # 规则 6:总敞口上限。
        total_ok = remaining_total > 0
        checks.append(
            RiskCheck(
                "total_exposure",
                total_ok,
                f"总剩余敞口 ${remaining_total:.2f}"
                f"（已用 ${current_total_exposure_usd:.2f} / 上限 ${limits.max_total_exposure_usd:.2f}）",
            )
        )

        # bankroll 分数上限检查（仅当启用 bankroll 时；解释核准规模是否受其约束）。
        if bcap != float("inf"):
            checks.append(
                RiskCheck(
                    "bankroll_fraction",
                    approved_size > 0,
                    f"bankroll 上限 ${bcap:.2f}"
                    f"（${limits.bankroll_usd:.2f} × {limits.max_bankroll_fraction:.0%}）",
                )
            )

        # 规则 7:余额充足(仅当提供余额时校验)——余额必须覆盖核准规模,不足则拦截。
        if available_balance_usd is not None:
            balance_ok = available_balance_usd >= approved_size and approved_size > 0
            checks.append(
                RiskCheck(
                    "balance",
                    balance_ok,
                    f"可用余额 ${available_balance_usd:.2f} "
                    f"{'≥' if balance_ok else '<'} 核准规模 ${approved_size:.2f}",
                )
            )

        approved = all(c.passed for c in checks) and approved_size > 0
        return RiskDecision(
            approved=approved,
            checks=checks,
            approved_size_usd=approved_size if approved else 0.0,
            dry_run=limits.dry_run,
            require_confirmation=limits.require_confirmation,
        )


__all__ = [
    "PriceKey",
    "RiskLimits",
    "RiskCheck",
    "RiskDecision",
    "RiskManager",
]
