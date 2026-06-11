"""明细查询端点测试（Phase 3 · 切片 K）。

用 TestClient 驱动 :func:`scanner.api.create_app`，验证三个明细端点：

- ``GET /markets/{platform}/{market_id}``：单个市场盘口明细；存在返回，不存在 404。
- ``GET /groups/{group_id}``：单个匹配组明细；存在返回，不存在 404。
- ``GET /signals/{group_id}``：单个活跃信号明细；未配置存储或不存在 404；
  且声明在 ``/signals/events`` 之后，不遮蔽事件端点。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.matching import MatchingEngine
from scanner.models import (
    ArbLeg,
    CanonicalMarket,
    Outcome,
    Signal,
    SignalStatus,
)
from scanner.signals import InMemorySignalStore
from scanner.store import InMemoryMarketStore

NOW = datetime.now(timezone.utc)


def _market(platform: str, market_id: str, title: str, *, yes: float = 0.4) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=[
            Outcome(name="YES", price=yes, bid=yes - 0.01, ask=yes + 0.01,
                    available_liquidity_usd=1000.0),
            Outcome(name="NO", price=1 - yes, bid=1 - yes - 0.01, ask=1 - yes + 0.01,
                    available_liquidity_usd=900.0),
        ],
        volume_usd=500.0,
        liquidity_usd=200.0,
        fee_rate=0.0,
        retrieved_at=NOW,
    )


# --------------------------------------------------------------------------- #
# 市场明细
# --------------------------------------------------------------------------- #

def test_market_detail_returns_orderbook():
    store = InMemoryMarketStore()
    store.upsert([_market("polymarket", "p1", "Alpha", yes=0.42)])
    client = TestClient(create_app(market_store=store))

    resp = client.get("/markets/polymarket/p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "polymarket"
    assert body["market_id"] == "p1"
    assert body["title"] == "Alpha"
    # 盘口：含 YES/NO 两个结果，且带 bid/ask/流动性。
    names = {o["name"] for o in body["outcomes"]}
    assert names == {"YES", "NO"}
    yes = next(o for o in body["outcomes"] if o["name"] == "YES")
    assert yes["bid"] is not None and yes["ask"] is not None
    assert yes["available_liquidity_usd"] == 1000.0
    # 新鲜度字段。
    assert "data_age_seconds" in body and "is_stale" in body


def test_market_detail_404_when_missing():
    store = InMemoryMarketStore()
    client = TestClient(create_app(market_store=store))
    resp = client.get("/markets/polymarket/nope")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 匹配组明细
# --------------------------------------------------------------------------- #

def test_group_detail_returns_group():
    # 两个互为等价的市场（标题相同），匹配引擎应分到同一组。
    store = InMemoryMarketStore()
    store.upsert([
        _market("polymarket", "p1", "Will BTC close above 100000 in 2025"),
        _market("predictfun", "1471", "Will BTC close above 100000 in 2025"),
    ])
    matcher = MatchingEngine(score_threshold=0.0)
    app = create_app(market_store=store, matching_engine=matcher)
    client = TestClient(app)

    groups = client.get("/groups").json()
    assert groups, "应至少有一个匹配组"
    gid = groups[0]["group_id"]

    resp = client.get(f"/groups/{gid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_id"] == gid
    assert len(body["members"]) >= 2
    assert "match_confidence" in body


def test_group_detail_404_when_missing():
    store = InMemoryMarketStore()
    client = TestClient(create_app(market_store=store))
    resp = client.get("/groups/does-not-exist")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 信号明细
# --------------------------------------------------------------------------- #

def _signal(group_id: str) -> Signal:
    return Signal(
        group_id=group_id,
        event_title="某事件",
        status=SignalStatus.SUSTAINED,
        legs=[ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4)],
        net_profit_margin=0.12,
        recommended_size_usd=100.0,
        peak_net_profit_margin=0.15,
        data_age_seconds=5.0,
        first_detected_at=NOW,
        last_seen_at=NOW,
    )


def test_signal_detail_returns_signal():
    sig_store = InMemorySignalStore()
    sig_store.upsert_active(_signal("g1"))
    app = create_app(market_store=InMemoryMarketStore(), signal_store=sig_store)
    client = TestClient(app)

    resp = client.get("/signals/g1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_id"] == "g1"
    assert body["status"] == "sustained"
    assert body["peak_net_profit_margin"] == 0.15


def test_signal_detail_404_when_missing():
    sig_store = InMemorySignalStore()
    app = create_app(market_store=InMemoryMarketStore(), signal_store=sig_store)
    client = TestClient(app)
    resp = client.get("/signals/unknown")
    assert resp.status_code == 404


def test_signal_detail_404_when_no_store():
    # 未配置信号存储时返回 404（而非空体）。
    app = create_app(market_store=InMemoryMarketStore())
    client = TestClient(app)
    resp = client.get("/signals/g1")
    assert resp.status_code == 404


def test_signal_events_not_shadowed_by_detail_route():
    # /signals/events 必须仍命中事件端点（返回 list），不被 /signals/{group_id} 遮蔽。
    sig_store = InMemorySignalStore()
    app = create_app(market_store=InMemoryMarketStore(), signal_store=sig_store)
    client = TestClient(app)
    resp = client.get("/signals/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
