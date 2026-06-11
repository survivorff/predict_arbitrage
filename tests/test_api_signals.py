"""信号相关 Read API 端点测试（Phase Two · 切片 A）。

用 ``fastapi`` 的 ``TestClient`` 驱动 ``create_app``，注入手工填充的
``InMemorySignalStore``，验证：

- GET /signals 返回活跃信号，按 net_profit_margin 降序，字段齐全。
- GET /signals 在未配置 signal_store 时返回 []。
- GET /signals/events 返回事件流，limit 只取最近 N 个。
- GET /signals/events 在未配置 signal_store 时返回 []。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.models import (
    ArbLeg,
    ArbitrageOpportunity,
    Signal,
    SignalEvent,
    SignalEventType,
    SignalStatus,
)
from scanner.signals import InMemorySignalStore, SignalService
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


def _signal(
    group_id: str,
    net_profit_margin: float,
    *,
    status: SignalStatus = SignalStatus.SUSTAINED,
    peak: float = None,
    event_title: str = "事件",
) -> Signal:
    """直接用模型构造一个活跃信号。"""
    return Signal(
        group_id=group_id,
        event_title=event_title,
        status=status,
        legs=[ArbLeg(platform="polymarket", market_id=f"{group_id}-m", outcome="YES", price=0.4)],
        net_profit_margin=net_profit_margin,
        recommended_size_usd=100.0,
        data_age_seconds=5.0,
        first_detected_at=BASE_TIME,
        last_seen_at=BASE_TIME + timedelta(seconds=30),
        peak_net_profit_margin=peak if peak is not None else net_profit_margin,
    )


# --------------------------------------------------------------------------- #
# GET /signals
# --------------------------------------------------------------------------- #

def test_signals_returns_active_sorted_desc_with_full_fields():
    # 活跃信号按 net_profit_margin 降序返回，字段齐全。
    store = InMemorySignalStore()
    store.upsert_active(_signal("g-low", 0.05))
    store.upsert_active(_signal("g-high", 0.30, status=SignalStatus.OPEN, peak=0.40))
    store.upsert_active(_signal("g-mid", 0.15))

    client = TestClient(
        create_app(market_store=InMemoryMarketStore(), signal_store=store)
    )
    resp = client.get("/signals")
    assert resp.status_code == 200
    body = resp.json()

    # 降序：0.30 > 0.15 > 0.05。
    assert [s["group_id"] for s in body] == ["g-high", "g-mid", "g-low"]
    assert [s["net_profit_margin"] for s in body] == [0.30, 0.15, 0.05]

    # 字段齐全性检查（取第一个）。
    top = body[0]
    for field in (
        "group_id",
        "event_title",
        "status",
        "legs",
        "net_profit_margin",
        "recommended_size_usd",
        "peak_net_profit_margin",
        "data_age_seconds",
        "first_detected_at",
        "last_seen_at",
        "closed_at",
        "duration_seconds",
    ):
        assert field in top
    assert top["status"] == "open"
    assert top["peak_net_profit_margin"] == 0.40
    # duration_seconds 从 first_detected_at 到 last_seen_at 为 30 秒。
    assert top["duration_seconds"] == 30.0
    # legs 字段含一条腿。
    assert len(top["legs"]) == 1
    assert top["legs"][0]["platform"] == "polymarket"


def test_signals_returns_empty_when_no_store_configured():
    # 未配置 signal_store 时返回空列表。
    client = TestClient(create_app(market_store=InMemoryMarketStore()))
    resp = client.get("/signals")
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# GET /signals/events
# --------------------------------------------------------------------------- #

def test_signal_events_returns_full_stream():
    # 事件流按发生顺序返回，字段齐全。
    store = InMemorySignalStore()
    store.append_event(
        SignalEvent(
            event_type=SignalEventType.OPENED,
            group_id="g1",
            event_title="事件",
            status=SignalStatus.OPEN,
            net_profit_margin=0.10,
            recommended_size_usd=100.0,
            peak_net_profit_margin=0.10,
            duration_seconds=0.0,
            occurred_at=BASE_TIME,
        )
    )
    store.append_event(
        SignalEvent(
            event_type=SignalEventType.UPDATED,
            group_id="g1",
            event_title="事件",
            status=SignalStatus.SUSTAINED,
            net_profit_margin=0.12,
            recommended_size_usd=100.0,
            peak_net_profit_margin=0.12,
            duration_seconds=10.0,
            occurred_at=BASE_TIME + timedelta(seconds=10),
        )
    )

    client = TestClient(
        create_app(market_store=InMemoryMarketStore(), signal_store=store)
    )
    resp = client.get("/signals/events")
    assert resp.status_code == 200
    body = resp.json()

    assert [e["event_type"] for e in body] == ["opened", "updated"]
    first = body[0]
    for field in (
        "event_type",
        "group_id",
        "event_title",
        "status",
        "net_profit_margin",
        "recommended_size_usd",
        "peak_net_profit_margin",
        "duration_seconds",
        "occurred_at",
    ):
        assert field in first


def test_signal_events_limit_returns_most_recent():
    # 用 SignalService.reconcile 填充 store，再用 limit 取最近 N 个事件。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_to_opp("g1", 0.10)])  # OPENED
    clock.advance(10)
    service.reconcile([_to_opp("g1", 0.12)])  # UPDATED
    clock.advance(10)
    service.reconcile([])  # CLOSED

    client = TestClient(
        create_app(market_store=InMemoryMarketStore(), signal_store=service.store)
    )

    # 全部 3 个事件。
    full = client.get("/signals/events").json()
    assert [e["event_type"] for e in full] == ["opened", "updated", "closed"]

    # limit=2 只返回最近 2 个。
    limited = client.get("/signals/events", params={"limit": 2}).json()
    assert [e["event_type"] for e in limited] == ["updated", "closed"]


def test_signal_events_returns_empty_when_no_store_configured():
    # 未配置 signal_store 时返回空列表。
    client = TestClient(create_app(market_store=InMemoryMarketStore()))
    resp = client.get("/signals/events")
    assert resp.status_code == 200
    assert resp.json() == []


def _to_opp(group_id: str, net_profit_margin: float) -> ArbitrageOpportunity:
    """构造机会快照，供 reconcile 填充信号存储。"""
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title="事件",
        legs=[ArbLeg(platform="polymarket", market_id=f"{group_id}-m", outcome="YES", price=0.4)],
        net_profit_margin=net_profit_margin,
        recommended_size_usd=100.0,
        detected_at=BASE_TIME,
        data_age_seconds=5.0,
    )
