"""API tests for the Read API (FastAPI) over a seeded in-memory store.

Drives ``create_app`` with a ``TestClient`` and asserts:

- ``/markets`` ranking order and threshold filtering (Req 3.3, 3.4, 3.5) plus
  freshness fields ``data_age_seconds`` and ``is_stale`` on every market (Req 8.1).
- ``/groups`` confidence filtering (Req 4.4, 4.5).
- ``/opportunities`` sorted by net margin descending with no non-positive
  margin, plus ``min_margin`` filtering and freshness fields (Req 5.5, 6.1, 8.1).
- ``/health`` per-adapter status, counts, and last successful cycle (Req 1.5).
- ``/config/alerts`` GET/PUT view and update (Req 6.2).

Validates: Property 10
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.config import AlertConfig, AlertCriteriaConfig
from scanner.models import (
    ArbLeg,
    ArbitrageOpportunity,
    CanonicalMarket,
    Outcome,
)
from scanner.opportunities import OpportunityService
from scanner.store import InMemoryMarketStore, OpportunityStore

NOW = datetime.now(timezone.utc)


def _market(
    platform: str,
    market_id: str,
    title: str,
    *,
    volume: float = None,
    liquidity: float = None,
    retrieved_at: datetime = None,
    is_stale: bool = False,
    outcomes=None,
) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=outcomes or [],
        volume_usd=volume,
        liquidity_usd=liquidity,
        retrieved_at=retrieved_at or NOW,
        is_stale=is_stale,
    )


def _opp(group_id: str, margin: float, *, data_age_seconds: float = 1.0) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=f"event {group_id}",
        legs=[ArbLeg(platform="polymarket", market_id="m1", outcome="YES", price=0.4)],
        net_profit_margin=margin,
        recommended_size_usd=100.0,
        detected_at=NOW,
        data_age_seconds=data_age_seconds,
    )


# ---------------------------------------------------------------------------
# Fixtures: a seeded store + client
# ---------------------------------------------------------------------------


@pytest.fixture
def market_store() -> InMemoryMarketStore:
    store = InMemoryMarketStore()
    store.upsert(
        [
            _market("polymarket", "p1", "Alpha", volume=500.0, liquidity=100.0),
            _market("kalshi", "k1", "Beta", volume=1000.0, liquidity=50.0),
            _market("polymarket", "p2", "Gamma", volume=200.0, liquidity=300.0),
        ]
    )
    return store


@pytest.fixture
def opportunity_service() -> OpportunityService:
    service = OpportunityService(store=OpportunityStore())
    service.reconcile(
        [
            _opp("low", 0.01),
            _opp("high", 0.25),
            _opp("mid", 0.10),
            _opp("zero", 0.0),
            _opp("neg", -0.1),
        ]
    )
    return service


@pytest.fixture
def alert_config() -> AlertConfig:
    return AlertConfig(criteria=AlertCriteriaConfig(min_net_profit_margin=0.02))


@pytest.fixture
def client(market_store, opportunity_service, alert_config) -> TestClient:
    app = create_app(
        market_store=market_store,
        opportunity_service=opportunity_service,
        alert_config=alert_config,
        staleness_threshold=60.0,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# /markets
# ---------------------------------------------------------------------------


def test_markets_sorted_by_volume_descending(client):
    resp = client.get("/markets", params={"sort": "volume"})
    assert resp.status_code == 200
    volumes = [m["volume_usd"] for m in resp.json()]
    assert volumes == [1000.0, 500.0, 200.0]


def test_markets_sorted_by_liquidity_descending(client):
    resp = client.get("/markets", params={"sort": "liquidity"})
    assert resp.status_code == 200
    liquidity = [m["liquidity_usd"] for m in resp.json()]
    assert liquidity == [300.0, 100.0, 50.0]


def test_markets_min_volume_filter(client):
    resp = client.get("/markets", params={"sort": "volume", "min_volume": 400})
    assert resp.status_code == 200
    ids = [m["market_id"] for m in resp.json()]
    assert ids == ["k1", "p1"]


def test_markets_min_liquidity_filter(client):
    resp = client.get("/markets", params={"sort": "liquidity", "min_liquidity": 100})
    assert resp.status_code == 200
    ids = [m["market_id"] for m in resp.json()]
    assert ids == ["p2", "p1"]


def test_markets_include_freshness_fields(client):
    resp = client.get("/markets")
    assert resp.status_code == 200
    for market in resp.json():
        assert "data_age_seconds" in market
        assert "is_stale" in market
        assert market["data_age_seconds"] >= 0


def test_markets_reports_stale_flag():
    store = InMemoryMarketStore()
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=300)
    store.upsert([_market("kalshi", "k1", "Stale", volume=10.0, retrieved_at=stale_time, is_stale=True)])
    client = TestClient(create_app(market_store=store))
    resp = client.get("/markets")
    body = resp.json()
    assert body[0]["is_stale"] is True
    assert body[0]["data_age_seconds"] > 60


def test_markets_invalid_sort_rejected(client):
    resp = client.get("/markets", params={"sort": "bogus"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /groups
# ---------------------------------------------------------------------------


def _binary(name_yes="YES", name_no="NO", price_yes=0.5, price_no=0.5):
    return [
        Outcome(name=name_yes, price=price_yes, ask=price_yes),
        Outcome(name=name_no, price=price_no, ask=price_no),
    ]


def test_groups_returns_matched_groups_with_confidence():
    store = InMemoryMarketStore()
    store.upsert(
        [
            _market(
                "polymarket",
                "p1",
                "Will Bitcoin close above 100000 in 2025",
                outcomes=_binary(),
            ),
            _market(
                "kalshi",
                "k1",
                "Bitcoin above 100000 by 2025",
                outcomes=_binary(),
            ),
        ]
    )
    client = TestClient(create_app(market_store=store))
    resp = client.get("/groups")
    assert resp.status_code == 200
    groups = resp.json()
    assert len(groups) >= 1
    for g in groups:
        assert 0.0 <= g["match_confidence"] <= 1.0


def test_groups_min_confidence_filter():
    store = InMemoryMarketStore()
    store.upsert(
        [
            _market(
                "polymarket",
                "p1",
                "Will Bitcoin close above 100000 in 2025",
                outcomes=_binary(),
            ),
            _market(
                "kalshi",
                "k1",
                "Bitcoin above 100000 by 2025",
                outcomes=_binary(),
            ),
        ]
    )
    client = TestClient(create_app(market_store=store))
    # An impossibly high confidence floor filters everything out.
    resp = client.get("/groups", params={"min_confidence": 1.0})
    assert resp.status_code == 200
    assert all(g["match_confidence"] >= 1.0 for g in resp.json())


# ---------------------------------------------------------------------------
# /opportunities
# ---------------------------------------------------------------------------


def test_opportunities_sorted_descending_no_nonpositive(client):
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    ids = [o["group_id"] for o in body]
    # Property 10: sorted by net margin desc, no margin <= 0.
    assert ids == ["high", "mid", "low"]
    margins = [o["net_profit_margin"] for o in body]
    assert margins == sorted(margins, reverse=True)
    assert all(o["net_profit_margin"] > 0 for o in body)


def test_opportunities_min_margin_filter(client):
    resp = client.get("/opportunities", params={"min_margin": 0.1})
    assert resp.status_code == 200
    ids = [o["group_id"] for o in resp.json()]
    assert ids == ["high", "mid"]


def test_opportunities_include_freshness_fields(client):
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    for opp in resp.json():
        assert "data_age_seconds" in opp
        assert "is_stale" in opp


def test_opportunities_stale_flag_from_age():
    service = OpportunityService(store=OpportunityStore())
    service.reconcile([_opp("fresh", 0.2, data_age_seconds=5.0), _opp("old", 0.3, data_age_seconds=120.0)])
    client = TestClient(
        create_app(
            market_store=InMemoryMarketStore(),
            opportunity_service=service,
            staleness_threshold=60.0,
        )
    )
    resp = client.get("/opportunities")
    by_id = {o["group_id"]: o for o in resp.json()}
    assert by_id["old"]["is_stale"] is True
    assert by_id["fresh"]["is_stale"] is False


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_reports_per_adapter_status_and_counts(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market_count"] == 3
    assert body["opportunity_count"] == 3  # low, high, mid (positive margins)
    adapters = {a["name"]: a for a in body["adapters"]}
    assert set(adapters) == {"polymarket", "kalshi"}
    assert adapters["polymarket"]["market_count"] == 2
    assert adapters["kalshi"]["market_count"] == 1
    for a in adapters.values():
        assert a["last_successful_cycle"] is not None
    assert body["status"] == "ok"


def test_health_degraded_when_all_markets_stale():
    store = InMemoryMarketStore()
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=300)
    store.upsert([_market("kalshi", "k1", "Stale", retrieved_at=stale_time, is_stale=True)])
    client = TestClient(create_app(market_store=store))
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["adapters"][0]["healthy"] is False


def test_health_uses_injected_provider():
    from scanner.api import AdapterHealth

    def provider():
        return [
            AdapterHealth(name="polymarket", healthy=True, market_count=5),
            AdapterHealth(name="kalshi", healthy=False, market_count=0, last_error="timeout"),
        ]

    client = TestClient(
        create_app(market_store=InMemoryMarketStore(), health_provider=provider)
    )
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    names = {a["name"]: a for a in body["adapters"]}
    assert names["kalshi"]["last_error"] == "timeout"


# ---------------------------------------------------------------------------
# /config/alerts
# ---------------------------------------------------------------------------


def test_get_alert_config_returns_current_criteria(client):
    resp = client.get("/config/alerts")
    assert resp.status_code == 200
    assert resp.json()["min_net_profit_margin"] == 0.02


def test_put_alert_config_updates_in_memory(client):
    new_criteria = {
        "min_net_profit_margin": 0.15,
        "min_match_confidence": 0.8,
        "platforms": ["polymarket"],
    }
    resp = client.put("/config/alerts", json=new_criteria)
    assert resp.status_code == 200
    assert resp.json()["min_net_profit_margin"] == 0.15

    # The update persists for subsequent reads.
    follow = client.get("/config/alerts")
    assert follow.json()["min_net_profit_margin"] == 0.15
    assert follow.json()["min_match_confidence"] == 0.8
    assert follow.json()["platforms"] == ["polymarket"]


# ---------------------------------------------------------------------------
# /config/sizing（bankroll/¼-Kelly 建议仓位配置，供前端计算建议投入）
# ---------------------------------------------------------------------------

def test_sizing_config_default(client):
    # 未传 sizing_config → 默认 bankroll=0（不约束）。
    r = client.get("/config/sizing")
    assert r.status_code == 200
    body = r.json()
    assert body["bankroll_usd"] == 0.0
    assert body["max_bankroll_fraction"] == 0.25


def test_sizing_config_custom(market_store, opportunity_service, alert_config):
    app = create_app(
        market_store=market_store,
        opportunity_service=opportunity_service,
        alert_config=alert_config,
        sizing_config={"bankroll_usd": 2000.0, "max_bankroll_fraction": 0.25},
    )
    c = TestClient(app)
    body = c.get("/config/sizing").json()
    assert body["bankroll_usd"] == 2000.0
    assert body["max_bankroll_fraction"] == 0.25
