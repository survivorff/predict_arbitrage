"""风控闸门测试（Phase 3 · 切片 G）。

逐条验证 :class:`scanner.risk.RiskManager` 的放行/拦截边界:
- 最小净利润率、信号新鲜度、滑点保护、单笔/单市场/总敞口上限、余额充足。
- 核准规模取各上限余量的最小值。
- dry-run / require_confirmation 标志透传。
- RiskDecision 可序列化。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.models import ArbLeg, ArbitrageOpportunity
from scanner.risk import RiskLimits, RiskManager

NOW = datetime.now(timezone.utc)


def _opp(
    *,
    margin: float = 0.10,
    size: float = 100.0,
    data_age: float = 5.0,
    legs=None,
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id="g1",
        event_title="某事件",
        legs=legs or [
            ArbLeg(platform="polymarket", market_id="p1", outcome="YES", price=0.40),
            ArbLeg(platform="predictfun", market_id="1471", outcome="NO", price=0.45),
        ],
        net_profit_margin=margin,
        recommended_size_usd=size,
        detected_at=NOW,
        data_age_seconds=data_age,
    )


# --------------------------------------------------------------------------- #
# 放行的基准用例
# --------------------------------------------------------------------------- #

def test_approves_healthy_opportunity():
    rm = RiskManager(RiskLimits())  # 默认:单笔100/市场200/总1000/滑点0.02/利润0.02/年龄30
    d = rm.evaluate(_opp(margin=0.10, size=100.0, data_age=5.0))
    assert d.approved is True
    assert d.approved_size_usd == 100.0
    assert all(c.passed for c in d.checks)
    assert d.dry_run is True and d.require_confirmation is True


# --------------------------------------------------------------------------- #
# 规则 1:最小净利润率
# --------------------------------------------------------------------------- #

def test_rejects_below_min_margin():
    rm = RiskManager(RiskLimits(min_net_profit_margin=0.05))
    d = rm.evaluate(_opp(margin=0.04))
    assert d.approved is False
    assert any(c.name == "min_net_profit_margin" and not c.passed for c in d.checks)
    assert d.approved_size_usd == 0.0


def test_margin_at_threshold_passes():
    rm = RiskManager(RiskLimits(min_net_profit_margin=0.05))
    d = rm.evaluate(_opp(margin=0.05))
    assert any(c.name == "min_net_profit_margin" and c.passed for c in d.checks)


# --------------------------------------------------------------------------- #
# 规则 2:信号新鲜度
# --------------------------------------------------------------------------- #

def test_rejects_stale_signal():
    rm = RiskManager(RiskLimits(max_data_age_seconds=30.0))
    d = rm.evaluate(_opp(data_age=45.0))
    assert d.approved is False
    assert any(c.name == "freshness" and not c.passed for c in d.checks)


def test_freshness_at_threshold_passes():
    rm = RiskManager(RiskLimits(max_data_age_seconds=30.0))
    d = rm.evaluate(_opp(data_age=30.0))
    assert any(c.name == "freshness" and c.passed for c in d.checks)


# --------------------------------------------------------------------------- #
# 规则 3:滑点保护
# --------------------------------------------------------------------------- #

def test_slippage_skipped_when_no_observed_prices():
    rm = RiskManager(RiskLimits())
    d = rm.evaluate(_opp())
    # 未提供观测价 -> 不产生 slippage 检查项。
    assert not any(c.name == "slippage" for c in d.checks)
    assert d.approved is True


def test_rejects_excessive_slippage():
    rm = RiskManager(RiskLimits(max_slippage=0.02))
    observed = {
        ("polymarket", "p1", "YES"): 0.40,   # 无偏离
        ("predictfun", "1471", "NO"): 0.50,  # 偏离 0.05 > 0.02
    }
    d = rm.evaluate(_opp(), observed_prices=observed)
    assert d.approved is False
    assert any(c.name == "slippage" and not c.passed for c in d.checks)


def test_allows_slippage_within_tolerance():
    rm = RiskManager(RiskLimits(max_slippage=0.02))
    observed = {
        ("polymarket", "p1", "YES"): 0.41,   # 偏离 0.01
        ("predictfun", "1471", "NO"): 0.455, # 偏离 0.005
    }
    d = rm.evaluate(_opp(), observed_prices=observed)
    assert any(c.name == "slippage" and c.passed for c in d.checks)
    assert d.approved is True


# --------------------------------------------------------------------------- #
# 规则 4/5/6:规模与敞口上限
# --------------------------------------------------------------------------- #

def test_approved_size_capped_by_max_trade_size():
    rm = RiskManager(RiskLimits(max_trade_size_usd=50.0))
    d = rm.evaluate(_opp(size=100.0))  # 建议 100,被单笔上限压到 50
    assert d.approved is True
    assert d.approved_size_usd == 50.0


def test_approved_size_capped_by_remaining_market_exposure():
    rm = RiskManager(RiskLimits(max_trade_size_usd=100.0, max_market_exposure_usd=80.0))
    d = rm.evaluate(_opp(size=100.0), market_exposure_usd=30.0)  # 市场剩余 50
    assert d.approved is True
    assert d.approved_size_usd == 50.0


def test_approved_size_capped_by_remaining_total_exposure():
    rm = RiskManager(RiskLimits(max_trade_size_usd=100.0, max_total_exposure_usd=500.0))
    d = rm.evaluate(_opp(size=100.0), current_total_exposure_usd=460.0)  # 总剩余 40
    assert d.approved is True
    assert d.approved_size_usd == 40.0


def test_rejects_when_market_exposure_exhausted():
    rm = RiskManager(RiskLimits(max_market_exposure_usd=100.0))
    d = rm.evaluate(_opp(size=100.0), market_exposure_usd=100.0)  # 已满
    assert d.approved is False
    assert any(c.name == "market_exposure" and not c.passed for c in d.checks)
    assert d.approved_size_usd == 0.0


def test_rejects_when_total_exposure_exhausted():
    rm = RiskManager(RiskLimits(max_total_exposure_usd=1000.0))
    d = rm.evaluate(_opp(size=100.0), current_total_exposure_usd=1000.0)
    assert d.approved is False
    assert any(c.name == "total_exposure" and not c.passed for c in d.checks)


# --------------------------------------------------------------------------- #
# 规则 7:余额充足
# --------------------------------------------------------------------------- #

def test_balance_skipped_when_not_provided():
    rm = RiskManager(RiskLimits())
    d = rm.evaluate(_opp())
    assert not any(c.name == "balance" for c in d.checks)


def test_rejects_when_insufficient_balance():
    rm = RiskManager(RiskLimits(max_trade_size_usd=100.0))
    d = rm.evaluate(_opp(size=100.0), available_balance_usd=40.0)
    assert d.approved is False
    assert any(c.name == "balance" and not c.passed for c in d.checks)
    # 核准规模 100（不被余额缩小）；余额 40 < 100 -> balance 检查拦截。


def test_sufficient_balance_passes():
    rm = RiskManager(RiskLimits(max_trade_size_usd=100.0))
    d = rm.evaluate(_opp(size=100.0), available_balance_usd=150.0)
    assert d.approved is True
    assert any(c.name == "balance" and c.passed for c in d.checks)
    assert d.approved_size_usd == 100.0


def test_zero_balance_rejects():
    rm = RiskManager(RiskLimits())
    d = rm.evaluate(_opp(size=100.0), available_balance_usd=0.0)
    assert d.approved is False
    # 余额为 0 -> 核准规模 0 -> 多条检查拦截。
    assert d.approved_size_usd == 0.0


# --------------------------------------------------------------------------- #
# 标志透传与序列化
# --------------------------------------------------------------------------- #

def test_dry_run_and_confirmation_flags_propagate():
    rm = RiskManager(RiskLimits(dry_run=False, require_confirmation=False))
    d = rm.evaluate(_opp())
    assert d.dry_run is False
    assert d.require_confirmation is False


def test_decision_is_serializable():
    rm = RiskManager(RiskLimits())
    d = rm.evaluate(_opp())
    snap = d.to_dict()
    assert snap["approved"] is True
    assert "checks" in snap and isinstance(snap["checks"], list)
    assert all({"name", "passed", "detail"} == set(c) for c in snap["checks"])


def test_rejected_reasons_lists_failures():
    rm = RiskManager(RiskLimits(min_net_profit_margin=0.5))
    d = rm.evaluate(_opp(margin=0.1))
    assert d.rejected_reasons
    assert any("min_net_profit_margin" in r for r in d.rejected_reasons)


def test_negative_limit_raises():
    with pytest.raises(ValueError):
        RiskLimits(max_trade_size_usd=-1.0)
