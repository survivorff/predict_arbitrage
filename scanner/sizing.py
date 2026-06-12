"""仓位建议:bankroll 分数上限（¼-Kelly 风格，P0 盈利核心）。

**诚实说明（设计取向）**：严格的跨平台套利若真「无风险」，理论上应在流动性/资金允许内
尽量做满。但它实际带有**残余风险**——leg risk（一腿成交另一腿失败 → 单边裸敞口）、
结算口径不一致（两边都输）、滑点。因此对每一笔「看似稳赚」的交易，用 bankroll 的一个
**分数**（默认 ¼）作为硬上限，防止单笔出错造成重大回撤——这正是业界「¼-Kelly」经验
法则的稳健化用法。

我们**刻意不**对套利套用方向性 Kelly 公式 `f* = edge/odds`：那需要对「套利失败概率」与
「失败时损失比例」的可靠估计，目前不具备，强行套用会给出过度自信的大仓位。改用更保守、
更可解释的「bankroll 分数上限 + 取各约束最小值」。详见 docs/15 ADR-006。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SizingSuggestion:
    """一次仓位建议的结果（可解释：建议额 + 各上限 + 绑定约束）。"""

    suggested_usd: float
    bankroll_cap_usd: Optional[float]  # bankroll×fraction；未启用 bankroll 时为 None
    binding_constraint: str            # 决定建议额的约束："bankroll"/"liquidity"/"risk_cap"/"none"


def bankroll_cap_usd(bankroll_usd: float, max_fraction: float) -> float:
    """bankroll 分数上限 = bankroll × max_fraction；未启用时返回 +inf（不约束）。"""
    if bankroll_usd <= 0 or max_fraction <= 0:
        return math.inf
    return bankroll_usd * max_fraction


def suggest_size(
    *,
    liquidity_cap_usd: float,
    bankroll_usd: float = 0.0,
    max_fraction: float = 0.25,
    risk_cap_usd: Optional[float] = None,
) -> SizingSuggestion:
    """给出建议仓位：取「流动性上限 / bankroll 分数上限 / 风控上限」三者的最小值。

    Args:
        liquidity_cap_usd: 可成交规模上限（最薄腿流动性）。
        bankroll_usd: 用户可投入本金；<=0 表示不启用 bankroll 上限。
        max_fraction: 单笔最多投入 bankroll 的比例（默认 0.25 = ¼-Kelly 风格）。
        risk_cap_usd: 风控核准上限（如单笔/敞口上限的余量）；None 表示不额外约束。

    Returns:
        :class:`SizingSuggestion`，``suggested_usd`` 为各上限最小值（>=0），并标注
        是哪个约束**绑定**了建议额，便于向用户解释「为什么是这个数」。
    """
    caps = {"liquidity": max(0.0, liquidity_cap_usd)}
    bcap = bankroll_cap_usd(bankroll_usd, max_fraction)
    if bcap != math.inf:
        caps["bankroll"] = max(0.0, bcap)
    if risk_cap_usd is not None:
        caps["risk_cap"] = max(0.0, risk_cap_usd)
    binding = min(caps, key=lambda k: caps[k])
    suggested = caps[binding]
    return SizingSuggestion(
        suggested_usd=round(suggested, 2),
        bankroll_cap_usd=(round(bcap, 2) if bcap != math.inf else None),
        binding_constraint=binding if suggested > 0 else "none",
    )


__all__ = ["SizingSuggestion", "bankroll_cap_usd", "suggest_size"]
