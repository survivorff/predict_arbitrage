"""历史时间序列端点测试（Phase 3 · 切片 K）。

用 TestClient 驱动 :func:`scanner.api.create_app`，验证两个历史端点：

- 未配置 history_store 时，``GET /opportunities/{gid}/history`` 与
  ``GET /markets/{plat}/{mid}/history`` 都返回 []。
- 配置了 SqliteHistoryStore(":memory:") 并预先 record 了点时，两个端点返回非空，
  字段为 {value, at, label}，at 为 isoformat 字符串，时间升序。
- limit query 参数生效。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.history import SqliteHistoryStore
from scanner.models import ArbLeg, ArbitrageOpportunity, CanonicalMarket, Outcome
from scanner.store import InMemoryMarketStore

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _opp(group_id: str, margin: float, *, event_title: str = "某事件") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=event_title,
        legs=[ArbLeg(platform="polymarket", market_id=f"{group_id}-m", outcome="YES", price=0.4)],
        net_profit_margin=margin,
        recommended_size_usd=100.0,
        detected_at=BASE_TIME,
        data_age_seconds=5.0,
    )


def _market(platform: str, market_id: str, price: float, *, title: str = "某市场") -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=[Outcome(name="YES", price=price)],
        retrieved_at=BASE_TIME,
    )


# --------------------------------------------------------------------------- #
# 未配置 history_store
# --------------------------------------------------------------------------- #

def test_history_endpoints_empty_when_no_store():
    # 未注入 history_store，两个端点都应返回 []。
    app = create_app(market_store=InMemoryMarketStore())
    client = TestClient(app)

    r1 = client.get("/opportunities/g1/history")
    assert r1.status_code == 200
    assert r1.json() == []

    r2 = client.get("/markets/polymarket/p1/history")
    assert r2.status_code == 200
    assert r2.json() == []


# --------------------------------------------------------------------------- #
# 配置了 history_store
# --------------------------------------------------------------------------- #

def _client_with_history():
    """构造带 SqliteHistoryStore(:memory:) 并预先记录点的 client，返回 (client, store)。"""
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    # 机会历史：三个时间点。
    store.record_opportunities([_opp("g1", 0.10, event_title="事件A")])
    clock.advance(60)
    store.record_opportunities([_opp("g1", 0.15, event_title="事件A")])
    clock.advance(60)
    store.record_opportunities([_opp("g1", 0.12, event_title="事件A")])
    # 市场历史：两个时间点（时钟已前进）。
    store.record_markets([_market("polymarket", "p1", 0.40, title="市场甲")])
    clock.advance(60)
    store.record_markets([_market("polymarket", "p1", 0.45, title="市场甲")])

    app = create_app(market_store=InMemoryMarketStore(), history_store=store)
    return TestClient(app), store


def test_opportunity_history_endpoint_returns_points():
    # 机会历史端点返回非空、字段为 {value, at, label}、at 为 isoformat、时间升序。
    client, store = _client_with_history()
    try:
        resp = client.get("/opportunities/g1/history")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        # 字段齐全。
        for point in body:
            assert set(point.keys()) == {"value", "at", "label"}
            assert point["label"] == "事件A"
            # at 为 isoformat 字符串，可被解析回 datetime。
            assert isinstance(point["at"], str)
            datetime.fromisoformat(point["at"])
        # value 按时间升序。
        assert [p["value"] for p in body] == [0.10, 0.15, 0.12]
        ats = [datetime.fromisoformat(p["at"]) for p in body]
        assert ats == sorted(ats)
    finally:
        store.close()


def test_market_history_endpoint_returns_points():
    # 市场历史端点返回非空、字段正确、时间升序。
    client, store = _client_with_history()
    try:
        resp = client.get("/markets/polymarket/p1/history")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        for point in body:
            assert set(point.keys()) == {"value", "at", "label"}
            assert point["label"] == "市场甲"
            datetime.fromisoformat(point["at"])
        assert [p["value"] for p in body] == [0.40, 0.45]
    finally:
        store.close()


def test_history_endpoints_respect_limit_query():
    # limit query 参数生效：只返回最近 N 个点（仍升序）。
    client, store = _client_with_history()
    try:
        r1 = client.get("/opportunities/g1/history", params={"limit": 2})
        assert r1.status_code == 200
        body1 = r1.json()
        assert len(body1) == 2
        # 最近两个为 0.15、0.12。
        assert [p["value"] for p in body1] == [0.15, 0.12]

        r2 = client.get("/markets/polymarket/p1/history", params={"limit": 1})
        assert r2.status_code == 200
        body2 = r2.json()
        assert len(body2) == 1
        assert body2[0]["value"] == 0.45
    finally:
        store.close()


def test_history_endpoints_empty_for_unknown_keys():
    # 未知 group/市场返回 []（已配置 store 但无数据）。
    client, store = _client_with_history()
    try:
        assert client.get("/opportunities/unknown/history").json() == []
        assert client.get("/markets/kalshi/zzz/history").json() == []
    finally:
        store.close()
