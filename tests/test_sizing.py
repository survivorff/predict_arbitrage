"""仓位建议测试（bankroll 分数上限 / ¼-Kelly 风格，P0）。

验证 :mod:`scanner.sizing`：
- bankroll 未启用（<=0）→ 不约束（+inf），建议受流动性/风控约束。
- bankroll 启用 → 单笔不超过 bankroll×fraction，并正确标注绑定约束。
- 取流动性/bankroll/风控三者最小值；负值/零被夹到 0。
"""

from __future__ import annotations

import math

from scanner.sizing import SizingSuggestion, bankroll_cap_usd, suggest_size


def test_bankroll_cap_disabled_returns_inf():
    assert bankroll_cap_usd(0.0, 0.25) == math.inf
    assert bankroll_cap_usd(1000.0, 0.0) == math.inf


def test_bankroll_cap_value():
    assert bankroll_cap_usd(2000.0, 0.25) == 500.0


def test_suggest_size_bound_by_bankroll():
    # 流动性 5000、风控 1000，但 bankroll 2000×25%=500 最小 → 绑定 bankroll。
    s = suggest_size(liquidity_cap_usd=5000.0, bankroll_usd=2000.0,
                     max_fraction=0.25, risk_cap_usd=1000.0)
    assert isinstance(s, SizingSuggestion)
    assert s.suggested_usd == 500.0
    assert s.bankroll_cap_usd == 500.0
    assert s.binding_constraint == "bankroll"


def test_suggest_size_bound_by_liquidity():
    # 流动性 80 最薄 → 绑定 liquidity。
    s = suggest_size(liquidity_cap_usd=80.0, bankroll_usd=2000.0,
                     max_fraction=0.25, risk_cap_usd=1000.0)
    assert s.suggested_usd == 80.0
    assert s.binding_constraint == "liquidity"


def test_suggest_size_bound_by_risk_cap():
    s = suggest_size(liquidity_cap_usd=5000.0, bankroll_usd=10000.0,
                     max_fraction=0.5, risk_cap_usd=100.0)
    assert s.suggested_usd == 100.0
    assert s.binding_constraint == "risk_cap"


def test_suggest_size_without_bankroll_uses_other_caps():
    # 未启用 bankroll → bankroll_cap_usd 为 None，受流动性约束。
    s = suggest_size(liquidity_cap_usd=300.0)
    assert s.suggested_usd == 300.0
    assert s.bankroll_cap_usd is None
    assert s.binding_constraint == "liquidity"


def test_suggest_size_zero_when_no_room():
    s = suggest_size(liquidity_cap_usd=0.0, bankroll_usd=1000.0, max_fraction=0.25)
    assert s.suggested_usd == 0.0
    assert s.binding_constraint == "none"
