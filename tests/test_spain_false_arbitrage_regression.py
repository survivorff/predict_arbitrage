"""回归测试：predict.fun 穿价脏挂单导致的虚假套利（"Spain win 2026 World Cup"）。

真实生产事故复盘：predict.fun 市场 1518（"Will Spain win the 2026 FIFA World Cup"）
的链下 CLOB 订单簿里混入一个**穿价**的陈旧买单（买价 0.78，远高于真实卖一 0.164）。
适配器朴素地取 ``best_bid = max(bids) = 0.78``，又按 ``NO ask = 1 - yes_bid`` 推导出
``NO ask = 0.22``。与 Polymarket Yes@0.16 配对 → 成本 0.38 → 报出 163% 的「无风险套利」，
而真实两平台 YES 都≈0.16，根本无套利。

根因：``PredictFunAdapter._book_levels`` 未对穿价（crossed book）做反穿价处理，
脏买单污染了 best_bid，进而污染 NO 的 ask 推导。

本测试**离线、确定性**（构造订单簿与 CanonicalMarket，不触达网络）：
1. 直接断言 ``_book_levels`` 对穿价订单簿返回干净的 best_bid/best_ask。
2. 端到端：把 predict.fun（NO ask≈0.836）与 Polymarket（YES 0.16 / NO 0.84）喂进
   MatchingEngine + ArbitrageEngine，断言净利润率不再是虚高值（应 < 0.05）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scanner.adapters.predictfun import PredictFunAdapter
from scanner.arbitrage import ArbitrageEngine
from scanner.matching import MatchingEngine
from scanner.models import CanonicalMarket, Outcome

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# 复刻市场 1518 真实订单簿的特征：盘口在 0.164/0.163，asks 尾部混有脏单
# （0.22/0.5/0.878/0.99…），并额外注入一个穿价的脏买单 0.78（事故根源）。
_DIRTY_ASKS = [
    [0.164, 50272.89],
    [0.165, 5559.83],
    [0.18, 39669.02],
    [0.22, 657097.1],
    [0.5, 310.75],
    [0.878, 10000],
    [0.99, 197.53],
    [0.998, 5000],
    [0.999, 103],
]
_DIRTY_BIDS = [
    [0.78, 5000],   # 穿价脏买单：远高于真实卖一 0.164，本应成交却残留在簿上。
    [0.163, 1801.24],
    [0.162, 11197.06],
    [0.16, 170589.33],
    [0.011, 100],
    [0.001, 103],
]


def test_book_levels_uncrosses_dirty_crossing_bid():
    """穿价脏买单不应污染 best_bid；反穿价后应回到真实盘口 0.163/0.164。"""
    adapter = PredictFunAdapter(base_url="https://api.predict.fun")
    best_bid, best_ask, _ = adapter._book_levels(
        {"asks": _DIRTY_ASKS, "bids": _DIRTY_BIDS}
    )
    assert best_ask == pytest.approx(0.164)
    # 修复前这里会是 0.78（脏单），修复后应为真实买一 0.163。
    assert best_bid == pytest.approx(0.163)


def test_clean_book_unaffected_by_uncross():
    """正常未穿价订单簿的最优买/卖价不应被反穿价逻辑改变。"""
    adapter = PredictFunAdapter(base_url="https://api.predict.fun")
    best_bid, best_ask, _ = adapter._book_levels(
        {"asks": [[0.63, 900]], "bids": [[0.61, 800]]}
    )
    assert best_bid == pytest.approx(0.61)
    assert best_ask == pytest.approx(0.63)


def _predictfun_spain_from_dirty_book() -> CanonicalMarket:
    """用脏订单簿规范化出 predict.fun 的 Spain 市场（NO ask 应≈0.836，而非 0.22）。"""
    adapter = PredictFunAdapter(base_url="https://api.predict.fun")
    yes_bid, yes_ask, ask_liq = adapter._book_levels(
        {"asks": _DIRTY_ASKS, "bids": _DIRTY_BIDS}
    )
    yes_price = (yes_bid + yes_ask) / 2.0
    no_bid = 1.0 - yes_ask
    no_ask = 1.0 - yes_bid
    return CanonicalMarket(
        platform="predictfun",
        market_id="1518",
        title="Will Spain win the 2026 FIFA World Cup",
        outcomes=[
            Outcome(name="YES", price=yes_price, bid=yes_bid, ask=yes_ask,
                    available_liquidity_usd=ask_liq),
            Outcome(name="NO", price=1.0 - yes_price, bid=no_bid, ask=no_ask,
                    available_liquidity_usd=ask_liq),
        ],
        retrieved_at=NOW,
        fee_rate=0.0,
    )


def _polymarket_spain() -> CanonicalMarket:
    """Polymarket 真实 Spain 市场：YES≈0.16 / NO≈0.84。"""
    return CanonicalMarket(
        platform="polymarket",
        market_id="0xSPAIN",
        title="Will Spain win the 2026 FIFA World Cup",
        outcomes=[
            Outcome(name="Yes", price=0.16, bid=0.159, ask=0.16,
                    available_liquidity_usd=5000.0),
            Outcome(name="No", price=0.84, bid=0.84, ask=0.841,
                    available_liquidity_usd=5000.0),
        ],
        retrieved_at=NOW,
        fee_rate=0.0,
    )


def test_spain_market_no_false_arbitrage_end_to_end():
    """端到端：两平台 YES 都≈0.16，匹配+套利引擎不应再报虚假高利润。"""
    pf = _predictfun_spain_from_dirty_book()
    poly = _polymarket_spain()

    # predict.fun NO 的 ask 必须是 ~0.836，而非事故中的 0.22。
    pf_no = next(o for o in pf.outcomes if o.name == "NO")
    assert pf_no.ask == pytest.approx(0.837)

    groups = MatchingEngine().match([pf, poly])
    assert len(groups) == 1, "两个同标题 Spain 市场应匹配为一组"

    engine = ArbitrageEngine(clock=lambda: NOW)
    opp = engine.evaluate_group(groups[0])
    assert opp is not None
    # 核心断言：净利润率不再是虚高的 1.63，应为合理的极小值（≈0.003 < 0.05）。
    assert opp.net_profit_margin < 0.05
