"""Unit tests for the KalshiAdapter (Req 1.1, 1.2, 2.1–2.5; Properties 1, 2).

Network is mocked with ``respx`` over recorded JSON fixtures. The adapter is
also run against the shared :class:`AdapterConformanceTests` suite to prove it
satisfies the extensibility contract (Req 7.2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx
import pytest
import respx

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.adapters.kalshi import KalshiAdapter
from scanner.fees import KalshiFeeModel
from scanner.models import FieldStatus
from tests.adapter_contract import AdapterConformanceTests

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://api.elections.kalshi.com"
MARKETS_URL = f"{BASE_URL}/trade-api/v2/markets"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as fh:
        return json.load(fh)


def _mock_markets(payload: dict) -> respx.Router:
    """Build a respx router that serves ``payload`` from the markets endpoint."""
    router = respx.mock(base_url=BASE_URL, assert_all_called=False)
    router.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=payload)
    )
    return router


# --------------------------------------------------------------------------- #
# Conformance suite: the KalshiAdapter must satisfy the shared contract.
# --------------------------------------------------------------------------- #
class TestKalshiConformance(AdapterConformanceTests):
    @pytest.fixture
    def adapter(self) -> PlatformAdapter:
        payload = _load_fixture("kalshi_markets.json")
        router = _mock_markets(payload)
        router.start()
        try:
            yield KalshiAdapter(base_url=BASE_URL)
        finally:
            router.stop()

    @pytest.fixture
    def slow_adapter(self) -> Optional[PlatformAdapter]:
        async def _slow(request: httpx.Request) -> httpx.Response:
            import asyncio

            await asyncio.sleep(5.0)
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        router = respx.mock(base_url=BASE_URL, assert_all_called=False)
        router.get("/trade-api/v2/markets").mock(side_effect=_slow)
        router.start()
        try:
            yield KalshiAdapter(base_url=BASE_URL)
        finally:
            router.stop()


# --------------------------------------------------------------------------- #
# Cents -> probability conversion (Req 2.2, Property 1).
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_cents_converted_to_probability(respx_mock):
    payload = _load_fixture("kalshi_markets.json")
    respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=payload)
    )

    markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    by_id = {m.market_id: m for m in markets}
    sp = by_id["INXD-23DEC29-B5000"]
    yes = next(o for o in sp.outcomes if o.name == "YES")
    no = next(o for o in sp.outcomes if o.name == "NO")

    # 63 cents last_price -> 0.63; bid 62 -> 0.62; ask 64 -> 0.64.
    assert yes.price == pytest.approx(0.63)
    assert yes.bid == pytest.approx(0.62)
    assert yes.ask == pytest.approx(0.64)
    # NO price is the complement of YES; its book comes from no_bid/no_ask.
    assert no.price == pytest.approx(0.37)
    assert no.bid == pytest.approx(0.36)
    assert no.ask == pytest.approx(0.38)


async def test_all_prices_within_unit_interval():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    for market in markets:
        for outcome in market.outcomes:
            assert 0.0 <= outcome.price <= 1.0
            if outcome.bid is not None:
                assert 0.0 <= outcome.bid <= 1.0
            if outcome.ask is not None:
                assert 0.0 <= outcome.ask <= 1.0


async def test_extreme_price_at_100_cents_clamped_to_one():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    extreme = next(m for m in markets if m.market_id == "CPI-24-EXTREME")
    yes = next(o for o in extreme.outcomes if o.name == "YES")
    no = next(o for o in extreme.outcomes if o.name == "NO")
    # 100 cents -> probability 1.0 exactly; NO complement is 0.0.
    assert yes.price == pytest.approx(1.0)
    assert no.price == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Magnitudes mapped to USD (Req 2.3, Property 2).
# --------------------------------------------------------------------------- #
async def test_volume_and_liquidity_converted_to_usd():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    sp = next(m for m in markets if m.market_id == "INXD-23DEC29-B5000")
    # Integer cents -> USD: 1234567 cents = $12,345.67; 4500000 cents = $45,000.
    assert sp.volume_usd == pytest.approx(12345.67)
    assert sp.liquidity_usd == pytest.approx(45000.0)
    assert sp.field_status["volume_usd"] is FieldStatus.OK
    assert sp.field_status["liquidity_usd"] is FieldStatus.OK


# --------------------------------------------------------------------------- #
# Missing-field handling (Req 2.4).
# --------------------------------------------------------------------------- #
async def test_missing_volume_and_liquidity_flagged_unavailable():
    payload = _load_fixture("kalshi_markets_missing_fields.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    novol = next(m for m in markets if m.market_id == "NOVOL-24-A")
    assert novol.volume_usd is None
    assert novol.liquidity_usd is None
    assert novol.field_status["volume_usd"] is FieldStatus.UNAVAILABLE
    assert novol.field_status["liquidity_usd"] is FieldStatus.UNAVAILABLE
    assert "volume_usd" in novol.unavailable_reasons
    assert "liquidity_usd" in novol.unavailable_reasons


async def test_bid_only_market_derives_price_from_bid():
    payload = _load_fixture("kalshi_markets_missing_fields.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    bid_only = next(m for m in markets if m.market_id == "BIDONLY-24-A")
    yes = next(o for o in bid_only.outcomes if o.name == "YES")
    # No last_price and no ask: price falls back to the bid (40 cents -> 0.40).
    assert yes.price == pytest.approx(0.40)
    assert yes.ask is None
    assert yes.bid == pytest.approx(0.40)


# --------------------------------------------------------------------------- #
# Canonical completeness (Req 2.1) and fee attachment (Req 2.5).
# --------------------------------------------------------------------------- #
async def test_canonical_market_has_required_fields():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    market = markets[0]
    assert market.platform == "kalshi"
    assert market.market_id
    assert market.title
    assert len(market.outcomes) == 2
    assert {o.name for o in market.outcomes} == {"YES", "NO"}
    assert market.retrieved_at is not None
    assert market.fee_rate is not None


async def test_fee_rate_matches_kalshi_fee_model():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    fee_model = KalshiFeeModel()
    sp = next(m for m in markets if m.market_id == "INXD-23DEC29-B5000")
    yes = next(o for o in sp.outcomes if o.name == "YES")
    expected = fee_model.fee_for(yes.price, 1.0)
    assert sp.fee_rate == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Auth handling (Req 7.1/7.3 graceful key handling).
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_api_key_sent_as_bearer_when_configured(respx_mock):
    payload = _load_fixture("kalshi_markets.json")
    route = respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=payload)
    )

    await KalshiAdapter(base_url=BASE_URL, api_key="secret-key").fetch_markets()

    request = route.calls.last.request
    assert request.headers.get("Authorization") == "Bearer secret-key"


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_missing_api_key_omits_auth_header(respx_mock, monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    payload = _load_fixture("kalshi_markets.json")
    route = respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=payload)
    )

    markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    # Public listing still works without a key (graceful handling).
    assert markets
    request = route.calls.last.request
    assert "Authorization" not in request.headers


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_api_key_read_from_environment(respx_mock, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY", "env-key")
    payload = _load_fixture("kalshi_markets.json")
    route = respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=payload)
    )

    await KalshiAdapter(base_url=BASE_URL).fetch_markets()

    request = route.calls.last.request
    assert request.headers.get("Authorization") == "Bearer env-key"


# --------------------------------------------------------------------------- #
# Pagination via cursor.
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_pagination_follows_cursor(respx_mock):
    page1 = {
        "cursor": "next-cursor",
        "markets": [
            {
                "ticker": "PG1-A",
                "title": "Page one market",
                "yes_bid": 10,
                "yes_ask": 12,
                "no_bid": 88,
                "no_ask": 90,
                "last_price": 11,
                "dollar_volume": 1000,
                "liquidity": 2000,
            }
        ],
    }
    page2 = {
        "cursor": "",
        "markets": [
            {
                "ticker": "PG2-A",
                "title": "Page two market",
                "yes_bid": 70,
                "yes_ask": 72,
                "no_bid": 28,
                "no_ask": 30,
                "last_price": 71,
                "dollar_volume": 3000,
                "liquidity": 4000,
            }
        ],
    }

    def _responder(request: httpx.Request) -> httpx.Response:
        if "cursor=next-cursor" in str(request.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    respx_mock.get("/trade-api/v2/markets").mock(side_effect=_responder)

    markets = await KalshiAdapter(base_url=BASE_URL).fetch_markets()
    ids = {m.market_id for m in markets}
    assert ids == {"PG1-A", "PG2-A"}


# --------------------------------------------------------------------------- #
# Error handling.
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_http_error_raises_adapter_error(respx_mock):
    respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    with pytest.raises(AdapterError):
        await KalshiAdapter(base_url=BASE_URL).fetch_markets()


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_missing_markets_key_raises_adapter_error(respx_mock):
    respx_mock.get("/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json={"cursor": ""})
    )

    with pytest.raises(AdapterError):
        await KalshiAdapter(base_url=BASE_URL).fetch_markets()


# --------------------------------------------------------------------------- #
# refresh_prices behavior.
# --------------------------------------------------------------------------- #
async def test_refresh_prices_updates_known_markets():
    payload = _load_fixture("kalshi_markets.json")
    with _mock_markets(payload):
        adapter = KalshiAdapter(base_url=BASE_URL)
        markets = await adapter.fetch_markets()
        refreshed = await adapter.refresh_prices(markets)

    assert len(refreshed) == len(markets)
    assert {m.market_id for m in refreshed} == {m.market_id for m in markets}
