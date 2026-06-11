"""Unit tests for the PolymarketAdapter (Req 1.1, 1.2, 2.1-2.5).

Network is mocked with ``respx`` over recorded JSON fixtures shaped like the
Polymarket Gamma and CLOB APIs. The suite asserts normalization correctness
(prices in [0,1], USD magnitudes, bid/ask population, flat fee rate) and that a
market missing a required field is flagged ``UNAVAILABLE`` with a reason
(Req 2.4). It also runs the PolymarketAdapter through the shared adapter
conformance suite (Req 7.2).

Validates: Property 1 (price bounds), Property 2 (non-negative magnitudes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import respx

from scanner.adapters.base import AdapterError
from scanner.adapters.polymarket import (
    CLOB_BASE_URL,
    GAMMA_BASE_URL,
    PolymarketAdapter,
)
from scanner.fees import FlatFeeModel
from scanner.models import CanonicalMarket, FieldStatus
from tests.adapter_contract import AdapterConformanceTests

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _mount(
    router: respx.MockRouter,
    gamma_markets: List[Dict[str, Any]],
    books: Dict[str, Any],
) -> None:
    """Mount Gamma markets and per-token CLOB book responses on the router."""
    router.get(f"{GAMMA_BASE_URL}/markets").mock(
        return_value=httpx.Response(200, json=gamma_markets)
    )

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = request.url.params.get("token_id")
        book = books.get(token_id)
        if book is None:
            return httpx.Response(200, json={"bids": [], "asks": []})
        return httpx.Response(200, json=book)

    router.get(f"{CLOB_BASE_URL}/book").mock(side_effect=_book_response)


@respx.mock
async def test_pagination_fetches_multiple_pages():
    """适配器用 limit/offset 翻页，覆盖超过单页的市场（offset 驱动不同页）。"""
    # 构造两页：page_size=2，第 1 页 offset=0 返回 2 条，第 2 页 offset=2 返回 1 条（终止）。
    def _market(cid: str) -> Dict[str, Any]:
        return {
            "conditionId": cid,
            "question": f"Q {cid}",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
            "volumeNum": 100.0,
            "liquidityNum": 50.0,
        }

    pages = {0: [_market("a"), _market("b")], 2: [_market("c")]}

    def _markets_responder(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        return httpx.Response(200, json=pages.get(offset, []))

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(side_effect=_markets_responder)
        router.get(f"{CLOB_BASE_URL}/book").mock(
            return_value=httpx.Response(200, json={"bids": [], "asks": []})
        )
        adapter = PolymarketAdapter(page_size=2, max_markets=10)
        markets = await adapter.fetch_markets()

    # 两页合并：a、b、c 三个市场。
    assert {m.market_id for m in markets} == {"a", "b", "c"}


@respx.mock
async def test_categories_fetch_by_tag_id():
    """配置类目时按 tag_id 逐类目抓取，并把类目写入 CanonicalMarket.category。"""
    from scanner.adapters.polymarket import CATEGORY_TAG_IDS

    def _market(cid: str) -> Dict[str, Any]:
        return {
            "conditionId": cid,
            "question": f"Q {cid}",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
        }

    seen_tags = []

    def _markets_responder(request: httpx.Request) -> httpx.Response:
        tag = request.url.params.get("tag_id")
        offset = int(request.url.params.get("offset", "0"))
        seen_tags.append(tag)
        if offset > 0:
            return httpx.Response(200, json=[])
        # 每个类目返回一个带该 tag 的市场。
        return httpx.Response(200, json=[_market(f"c{tag}")])

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(side_effect=_markets_responder)
        router.get(f"{CLOB_BASE_URL}/book").mock(
            return_value=httpx.Response(200, json={"bids": [], "asks": []})
        )
        adapter = PolymarketAdapter(
            page_size=50, max_markets=10, categories=["politics", "sports"]
        )
        markets = await adapter.fetch_markets()

    # 两个类目各按其 tag_id 查询。
    assert str(CATEGORY_TAG_IDS["politics"]) in seen_tags
    assert str(CATEGORY_TAG_IDS["sports"]) in seen_tags
    # 市场带上来源类目。
    cats = {m.category for m in markets}
    assert cats == {"politics", "sports"}


@respx.mock
async def test_unknown_categories_ignored():
    """未知类目 slug 被忽略，退化为全站抓取（不带 tag_id）。"""
    def _market(cid: str) -> Dict[str, Any]:
        return {
            "conditionId": cid, "question": "Q", "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
        }

    used_tag = {"has_tag": False}

    def _responder(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("tag_id"):
            used_tag["has_tag"] = True
        offset = int(request.url.params.get("offset", "0"))
        return httpx.Response(200, json=[_market("x")] if offset == 0 else [])

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(side_effect=_responder)
        router.get(f"{CLOB_BASE_URL}/book").mock(
            return_value=httpx.Response(200, json={"bids": [], "asks": []})
        )
        adapter = PolymarketAdapter(categories=["nonexistent-category"])
        markets = await adapter.fetch_markets()

    assert used_tag["has_tag"] is False  # 全站抓取，不带 tag_id
    assert len(markets) == 1


@respx.mock
async def test_max_markets_caps_pagination():
    """max_markets 限制总抓取量（即使还有更多页）。"""
    def _market(cid: str) -> Dict[str, Any]:
        return {
            "conditionId": cid,
            "question": f"Q {cid}",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps([f"{cid}-y", f"{cid}-n"]),
        }

    def _markets_responder(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        # 每页都返回满页（page_size=2），制造"无限"页。
        return httpx.Response(200, json=[_market(f"m{offset}"), _market(f"m{offset}b")])

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(side_effect=_markets_responder)
        router.get(f"{CLOB_BASE_URL}/book").mock(
            return_value=httpx.Response(200, json={"bids": [], "asks": []})
        )
        adapter = PolymarketAdapter(page_size=2, max_markets=3)
        markets = await adapter.fetch_markets()

    # max_markets=3 → 最多 3 个市场。
    assert len(markets) == 3


# --------------------------------------------------------------------------- #
# Normalization behaviour
# --------------------------------------------------------------------------- #
@respx.mock
async def test_fetch_markets_normalizes_gamma_and_clob():
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    assert len(markets) == 2
    by_id = {m.market_id: m for m in markets}
    election = by_id["0xcond-election-2024"]

    assert election.platform == "polymarket"
    assert election.title == "Will Candidate A win the 2024 election?"
    assert [o.name for o in election.outcomes] == ["Yes", "No"]

    # Prices come straight from Gamma (already 0..1, Req 2.2).
    yes, no = election.outcomes
    assert yes.price == pytest.approx(0.62)
    assert no.price == pytest.approx(0.38)

    # Bid/ask populated from the CLOB book for spread cost.
    assert yes.bid == pytest.approx(0.61)  # best (max) bid
    assert yes.ask == pytest.approx(0.63)  # best (min) ask
    assert yes.available_liquidity_usd == pytest.approx(900 * 0.63)

    # Volume/liquidity mapped to USD (Req 2.3).
    assert election.volume_usd == pytest.approx(1543210.5)
    assert election.liquidity_usd == pytest.approx(84500.0)
    assert election.field_status["volume_usd"] is FieldStatus.OK
    assert election.field_status["liquidity_usd"] is FieldStatus.OK


@respx.mock
async def test_fee_rate_from_flat_fee_model():
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter(fee_model=FlatFeeModel(0.0))
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    for market in markets:
        assert market.fee_rate == 0.0


@respx.mock
async def test_custom_flat_fee_rate_is_applied():
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter(fee_model=FlatFeeModel(0.02))
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    assert markets
    for market in markets:
        assert market.fee_rate == pytest.approx(0.02)


@respx.mock
async def test_all_prices_within_unit_interval():
    # Property 1: every produced price/bid/ask is in [0, 1].
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    for market in markets:
        for outcome in market.outcomes:
            assert 0.0 <= outcome.price <= 1.0
            if outcome.bid is not None:
                assert 0.0 <= outcome.bid <= 1.0
            if outcome.ask is not None:
                assert 0.0 <= outcome.ask <= 1.0


@respx.mock
async def test_magnitudes_non_negative():
    # Property 2: volume/liquidity are None or >= 0.
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    for market in markets:
        if market.volume_usd is not None:
            assert market.volume_usd >= 0.0
        if market.liquidity_usd is not None:
            assert market.liquidity_usd >= 0.0
        for outcome in market.outcomes:
            if outcome.available_liquidity_usd is not None:
                assert outcome.available_liquidity_usd >= 0.0


# --------------------------------------------------------------------------- #
# Missing-field handling (Req 2.4)
# --------------------------------------------------------------------------- #
@respx.mock
async def test_missing_volume_flagged_unavailable():
    gamma = _load("polymarket_gamma_missing_field.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    assert len(markets) == 1
    market = markets[0]
    # Volume is absent from the source -> UNAVAILABLE with a recorded reason.
    assert market.volume_usd is None
    assert market.field_status["volume_usd"] is FieldStatus.UNAVAILABLE
    assert market.unavailable_reasons["volume_usd"]
    # Liquidity is present -> OK.
    assert market.liquidity_usd == pytest.approx(5000.0)
    assert market.field_status["liquidity_usd"] is FieldStatus.OK


@respx.mock
async def test_outcome_without_price_or_book_flagged_unavailable():
    gamma = [
        {
            "conditionId": "0xcond-no-price",
            "question": "Will it happen?",
            "outcomes": "[\"Yes\", \"No\"]",
            "outcomePrices": "[]",
            "clobTokenIds": "[\"400001\", \"400002\"]",
            "volumeNum": 100.0,
            "liquidityNum": 50.0,
        }
    ]
    # Empty books for these tokens -> no price recoverable.
    books: Dict[str, Any] = {}
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    assert len(markets) == 1
    market = markets[0]
    assert market.outcomes == []
    assert market.field_status["outcomes"] is FieldStatus.UNAVAILABLE
    assert "outcomes[0].price" in market.field_status
    assert market.field_status["outcomes[0].price"] is FieldStatus.UNAVAILABLE


@respx.mock
async def test_price_falls_back_to_book_mid_when_gamma_omits():
    gamma = [
        {
            "conditionId": "0xcond-book-mid",
            "question": "Mid fallback?",
            "outcomes": "[\"Yes\", \"No\"]",
            "outcomePrices": "[]",
            "clobTokenIds": "[\"100001\", \"100002\"]",
            "volumeNum": 100.0,
            "liquidityNum": 50.0,
        }
    ]
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    yes = markets[0].outcomes[0]
    # Book mid for token 100001 = (0.61 + 0.63) / 2 = 0.62
    assert yes.price == pytest.approx(0.62)


# --------------------------------------------------------------------------- #
# Payload shape + error handling
# --------------------------------------------------------------------------- #
@respx.mock
async def test_handles_data_wrapped_payload():
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(
            return_value=httpx.Response(200, json={"data": gamma})
        )

        def _book_response(request: httpx.Request) -> httpx.Response:
            token_id = request.url.params.get("token_id")
            return httpx.Response(200, json=books.get(token_id, {"bids": [], "asks": []}))

        router.get(f"{CLOB_BASE_URL}/book").mock(side_effect=_book_response)

        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        await adapter.aclose()

    assert len(markets) == 2


@respx.mock
async def test_http_error_raises_adapter_error():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(
            return_value=httpx.Response(500, text="boom")
        )
        adapter = PolymarketAdapter()
        with pytest.raises(AdapterError):
            await adapter.fetch_markets()
        await adapter.aclose()


@respx.mock
async def test_transport_error_resets_client_for_self_heal():
    # 稳定性修复：传输层错误（休眠后连接池失效）应重置 client，下次请求用新连接自愈。
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{GAMMA_BASE_URL}/markets").mock(
            side_effect=httpx.ConnectError("connection failed")
        )
        adapter = PolymarketAdapter()
        with pytest.raises(AdapterError):
            await adapter.fetch_markets()
        # client 已被重置（置 None），下次请求会重建新连接。
        assert adapter._client is None


@respx.mock
async def test_refresh_prices_rereads_book_and_restamps():
    gamma = _load("polymarket_gamma_markets.json")
    books = _load("polymarket_clob_books.json")
    with respx.mock(assert_all_called=False) as router:
        _mount(router, gamma, books)
        adapter = PolymarketAdapter()
        markets = await adapter.fetch_markets()
        original = markets[0].retrieved_at
        refreshed = await adapter.refresh_prices(markets)
        await adapter.aclose()

    assert len(refreshed) == len(markets)
    for market in refreshed:
        assert isinstance(market, CanonicalMarket)
        assert market.retrieved_at >= original
        for outcome in market.outcomes:
            assert 0.0 <= outcome.price <= 1.0


# --------------------------------------------------------------------------- #
# Adapter conformance suite (Req 7.2)
# --------------------------------------------------------------------------- #
class TestPolymarketConformance(AdapterConformanceTests):
    """Run the shared conformance checks against the PolymarketAdapter.

    A respx router is mounted for the duration of each adapter call via a
    wrapping client; the adapter fixture returns an adapter backed by an
    httpx client whose transport is patched by respx.
    """

    @pytest.fixture
    def adapter(self):
        gamma = _load("polymarket_gamma_markets.json")
        books = _load("polymarket_clob_books.json")
        router = respx.mock(assert_all_called=False)
        _mount(router, gamma, books)
        router.start()
        try:
            yield PolymarketAdapter()
        finally:
            router.stop()
            router.reset()

    @pytest.fixture
    def slow_adapter(self) -> Optional[PolymarketAdapter]:
        router = respx.mock(assert_all_called=False)

        async def _slow(request: httpx.Request) -> httpx.Response:
            import asyncio

            await asyncio.sleep(5.0)
            return httpx.Response(200, json=[])

        router.get(f"{GAMMA_BASE_URL}/markets").mock(side_effect=_slow)
        router.start()
        try:
            yield PolymarketAdapter()
        finally:
            router.stop()
            router.reset()
