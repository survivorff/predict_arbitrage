"""PredictFunAdapter 单元测试（Req 1.1, 1.2, 2.1–2.5, 7.2；Property 1, 2, 3）。

predict.fun 只读行情适配器：游标分页拉取 ``/v1/markets``，逐市场读
``/v1/markets/{id}/orderbook``，把 YES/NO bid/ask 规范化为 [0,1] 隐含概率，并把
平台自报的 ``polymarketConditionIds`` / ``kalshiMarketTicker`` 写入 ``cross_refs``
作为金标准跨平台关联。网络用 respx mock，不触达真实网络。

适配器也跑共享的 :class:`AdapterConformanceTests` 套件，证明满足扩展契约（Req 7.2）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx
import pytest
import respx

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.adapters.predictfun import (
    DEFAULT_BASE_URL,
    PredictFunAdapter,
)
from scanner.models import FieldStatus
from tests.adapter_contract import AdapterConformanceTests

BASE_URL = "https://api.predict.fun"


# --------------------------------------------------------------------------- #
# 内联 fixture 数据与 respx 路由搭建辅助
# --------------------------------------------------------------------------- #
def _market_raw(
    *,
    market_id: Any = 1,
    question: Optional[str] = "Will BTC close above $100k by 2025?",
    title: Optional[str] = None,
    fee_rate_bps: Optional[int] = 100,
    polymarket_ids: Optional[List[str]] = None,
    kalshi_ticker: Optional[str] = "KX-T",
) -> Dict[str, Any]:
    """构造一个原始 predict.fun 市场 dict（仅含被规范化逻辑读取的字段）。"""
    raw: Dict[str, Any] = {"id": market_id}
    if question is not None:
        raw["question"] = question
    if title is not None:
        raw["title"] = title
    if fee_rate_bps is not None:
        raw["feeRateBps"] = fee_rate_bps
    if polymarket_ids is not None:
        raw["polymarketConditionIds"] = polymarket_ids
    if kalshi_ticker is not None:
        raw["kalshiMarketTicker"] = kalshi_ticker
    return raw


def _markets_page(data: List[Dict[str, Any]], cursor: Any = None) -> Dict[str, Any]:
    return {"success": True, "cursor": cursor, "data": data}


def _orderbook_payload(
    asks: List[List[float]], bids: List[List[float]], market_id: Any = 1
) -> Dict[str, Any]:
    return {
        "success": True,
        "data": {"marketId": market_id, "asks": asks, "bids": bids},
    }


# 默认订单簿：YES best_bid=0.61, best_ask=0.63 → YES 价 0.62；ask 流动性 0.63*900。
_DEFAULT_ORDERBOOK = _orderbook_payload(asks=[[0.63, 900]], bids=[[0.61, 800]])


def _orderbook_responder(books: Dict[str, Dict[str, Any]]):
    """按市场 id 返回订单簿；``books`` 缺失的 id → 404。"""

    def _responder(request: httpx.Request) -> httpx.Response:
        match = re.search(r"/v1/markets/([^/]+)/orderbook", request.url.path)
        market_id = match.group(1) if match else ""
        if market_id in books:
            return httpx.Response(200, json=books[market_id])
        return httpx.Response(404, json={"success": False})

    return _responder


def _mock(
    *,
    pages: List[Dict[str, Any]],
    books: Dict[str, Dict[str, Any]],
    orderbook_status: Optional[int] = None,
) -> respx.Router:
    """构造一个 respx 路由：markets 列表（支持多页游标）+ 订单簿。

    ``pages`` 为按序返回的市场页响应；``books`` 为 id->订单簿 payload；
    ``orderbook_status`` 若给定则订单簿统一返回该 HTTP 状态码（用于错误注入）。
    """
    router = respx.mock(base_url=BASE_URL, assert_all_called=False)

    # markets 列表：按游标顺序返回各页。
    state = {"index": 0}

    def _markets_responder(request: httpx.Request) -> httpx.Response:
        idx = min(state["index"], len(pages) - 1)
        state["index"] += 1
        return httpx.Response(200, json=pages[idx])

    router.get(path__regex=r"^/v1/markets/[^/]+/orderbook$").mock(
        side_effect=(
            (lambda request: httpx.Response(orderbook_status, json={"success": False}))
            if orderbook_status is not None
            else _orderbook_responder(books)
        )
    )
    router.get(path__regex=r"^/v1/markets$").mock(side_effect=_markets_responder)
    return router


# --------------------------------------------------------------------------- #
# 契约套件：PredictFunAdapter 必须满足共享 PlatformAdapter 契约（Req 7.2）。
# --------------------------------------------------------------------------- #
class TestPredictFunConformance(AdapterConformanceTests):
    @pytest.fixture
    def adapter(self) -> PlatformAdapter:
        router = _mock(
            pages=[_markets_page([_market_raw()], cursor=None)],
            books={"1": _DEFAULT_ORDERBOOK},
        )
        router.start()
        try:
            yield PredictFunAdapter(base_url=BASE_URL)
        finally:
            router.stop()

    @pytest.fixture
    def slow_adapter(self) -> Optional[PlatformAdapter]:
        async def _slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5.0)
            return httpx.Response(200, json=_markets_page([], cursor=None))

        router = respx.mock(base_url=BASE_URL, assert_all_called=False)
        router.get(path__regex=r"^/v1/markets$").mock(side_effect=_slow)
        router.start()
        try:
            yield PredictFunAdapter(base_url=BASE_URL)
        finally:
            router.stop()


# --------------------------------------------------------------------------- #
# 规范化：价格/费率/cross_refs/字段状态（Req 2.1–2.5）。
# --------------------------------------------------------------------------- #
async def test_default_base_url_constant():
    assert DEFAULT_BASE_URL == "https://api.predict.fun"


async def test_fetch_normalizes_market_basics():
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    assert len(markets) == 1
    market = markets[0]
    assert market.platform == "predictfun"
    assert market.market_id == "1"
    assert market.title == "Will BTC close above $100k by 2025?"


async def test_yes_no_prices_from_orderbook():
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    market = markets[0]
    yes = next(o for o in market.outcomes if o.name == "YES")
    no = next(o for o in market.outcomes if o.name == "NO")

    # YES 价 = mid(0.61, 0.63) = 0.62；bid/ask 取自最优买/卖。
    assert yes.price == pytest.approx(0.62)
    assert yes.bid == pytest.approx(0.61)
    assert yes.ask == pytest.approx(0.63)
    # NO 价 = 1 - YES 价；NO bid = 1 - yes_ask；NO ask = 1 - yes_bid。
    assert no.price == pytest.approx(0.38)
    assert no.bid == pytest.approx(0.37)
    assert no.ask == pytest.approx(0.39)


async def test_fee_rate_from_bps():
    router = _mock(
        pages=[_markets_page([_market_raw(fee_rate_bps=100)], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    # 100 bps / 10000 = 0.01。
    assert markets[0].fee_rate == pytest.approx(0.01)


async def test_cross_refs_populated():
    router = _mock(
        pages=[
            _markets_page(
                [_market_raw(polymarket_ids=["0xPOLY"], kalshi_ticker="KX-T")],
                cursor=None,
            )
        ],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    assert markets[0].cross_refs == {"polymarket": ["0xPOLY"], "kalshi": ["KX-T"]}


async def test_volume_unavailable_liquidity_from_depth():
    # 成交量当前 API 不提供 → 不可用；流动性从订单簿深度估算 → 可用。
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    market = markets[0]
    # 成交量仍不可用。
    assert market.volume_usd is None
    assert market.field_status["volume_usd"] is FieldStatus.UNAVAILABLE
    # 流动性 = Σ price×size 两侧 = 0.63*900 + 0.61*800 = 567 + 488 = 1055。
    assert market.liquidity_usd == pytest.approx(0.63 * 900 + 0.61 * 800)
    assert market.field_status["liquidity_usd"] is FieldStatus.OK


async def test_liquidity_unavailable_when_orderbook_empty():
    # 订单簿为空时流动性也不可用（诚实降级）。
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _orderbook_payload(asks=[], bids=[])},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    market = markets[0]
    assert market.liquidity_usd is None
    assert market.field_status["liquidity_usd"] is FieldStatus.UNAVAILABLE


async def test_volume_extracted_from_stats_when_present():
    # 防御性：若将来 API 在 stats 填充成交量，则自动提取生效。
    raw = _market_raw()
    raw["stats"] = {"volume24h": 12345.6}
    router = _mock(
        pages=[_markets_page([raw], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    market = markets[0]
    assert market.volume_usd == pytest.approx(12345.6)
    assert market.field_status["volume_usd"] is FieldStatus.OK


async def test_all_prices_within_unit_interval():
    # Property 1：所有价格/bid/ask 都在 [0, 1]。
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    for market in markets:
        for outcome in market.outcomes:
            assert 0.0 <= outcome.price <= 1.0
            if outcome.bid is not None:
                assert 0.0 <= outcome.bid <= 1.0
            if outcome.ask is not None:
                assert 0.0 <= outcome.ask <= 1.0


# --------------------------------------------------------------------------- #
# 标题兜底与跳过（Req 2.1）。
# --------------------------------------------------------------------------- #
async def test_title_falls_back_to_title_field():
    router = _mock(
        pages=[
            _markets_page(
                [_market_raw(question=None, title="BTC above 100k")], cursor=None
            )
        ],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    assert len(markets) == 1
    assert markets[0].title == "BTC above 100k"


async def test_market_without_question_or_title_is_skipped():
    router = _mock(
        pages=[
            _markets_page(
                [
                    _market_raw(market_id=1, question=None, title=None),
                    _market_raw(market_id=2, question="Has a title"),
                ],
                cursor=None,
            )
        ],
        books={"1": _DEFAULT_ORDERBOOK, "2": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    # 无 question/title 的市场被过滤掉，仅剩 id=2。
    assert [m.market_id for m in markets] == ["2"]


# --------------------------------------------------------------------------- #
# 订单簿缺失/失败：市场仍被摄取但价格缺失（优雅降级）。
# --------------------------------------------------------------------------- #
async def test_empty_orderbook_yields_no_outcomes():
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _orderbook_payload(asks=[], bids=[])},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    assert len(markets) == 1
    market = markets[0]
    assert market.outcomes == []
    assert market.field_status["outcomes"] is FieldStatus.UNAVAILABLE


async def test_orderbook_http_500_market_still_ingested_without_prices():
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={},
        orderbook_status=500,
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    # 订单簿 500 不致命：市场仍出现，仅价格缺失。
    assert len(markets) == 1
    assert markets[0].outcomes == []
    assert markets[0].field_status["outcomes"] is FieldStatus.UNAVAILABLE


# --------------------------------------------------------------------------- #
# markets 端点错误 → AdapterError。
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_markets_http_500_raises_adapter_error(respx_mock):
    respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(500, json={"success": False})
    )

    with pytest.raises(AdapterError):
        await PredictFunAdapter(base_url=BASE_URL).fetch_markets()


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_markets_missing_data_raises_adapter_error(respx_mock):
    respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(200, json={"success": True, "cursor": None})
    )

    with pytest.raises(AdapterError):
        await PredictFunAdapter(base_url=BASE_URL).fetch_markets()


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_transport_error_resets_client_for_self_heal(respx_mock):
    # 稳定性修复：传输层错误（如休眠后连接池失效）应重置 client，使下次请求用新连接自愈。
    respx_mock.get(path__regex=r"^/v1/markets$").mock(
        side_effect=httpx.ConnectError("connection failed")
    )
    adapter = PredictFunAdapter(base_url=BASE_URL)
    # 触发一次传输失败。
    import pytest as _pytest
    with _pytest.raises(AdapterError):
        await adapter.fetch_markets()
    # client 已被重置（置 None），下次请求会重建。
    assert adapter._client is None


# --------------------------------------------------------------------------- #
# 鉴权头处理（Req 7.1/7.3 优雅降级）。
# --------------------------------------------------------------------------- #
@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_api_key_sent_as_header_when_configured(respx_mock):
    route = respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(200, json=_markets_page([], cursor=None))
    )

    await PredictFunAdapter(base_url=BASE_URL, api_key="secret").fetch_markets()

    request = route.calls.last.request
    assert request.headers.get("x-api-key") == "secret"


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_missing_api_key_omits_header(respx_mock, monkeypatch):
    monkeypatch.delenv("PREDICTFUN_API_KEY", raising=False)
    route = respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(200, json=_markets_page([], cursor=None))
    )

    markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    # 无 key 仍可公开读取（优雅降级）。
    assert markets == []
    request = route.calls.last.request
    assert "x-api-key" not in request.headers


# --------------------------------------------------------------------------- #
# 游标分页。
# --------------------------------------------------------------------------- #
async def test_pagination_merges_pages():
    router = _mock(
        pages=[
            _markets_page([_market_raw(market_id=1)], cursor="next"),
            _markets_page([_market_raw(market_id=2)], cursor=""),
        ],
        books={"1": _DEFAULT_ORDERBOOK, "2": _DEFAULT_ORDERBOOK},
    )
    with router:
        markets = await PredictFunAdapter(base_url=BASE_URL).fetch_markets()

    assert {m.market_id for m in markets} == {"1", "2"}


# --------------------------------------------------------------------------- #
# refresh_prices。
# --------------------------------------------------------------------------- #
async def test_refresh_prices_rereads_and_restamps():
    router = _mock(
        pages=[_markets_page([_market_raw()], cursor=None)],
        books={"1": _DEFAULT_ORDERBOOK},
    )
    with router:
        adapter = PredictFunAdapter(base_url=BASE_URL)
        markets = await adapter.fetch_markets()
        before = markets[0].retrieved_at
        refreshed = await adapter.refresh_prices(markets)

    assert len(refreshed) == len(markets)
    market = refreshed[0]
    assert market.retrieved_at >= before
    for outcome in market.outcomes:
        assert 0.0 <= outcome.price <= 1.0
        if outcome.bid is not None:
            assert 0.0 <= outcome.bid <= 1.0
        if outcome.ask is not None:
            assert 0.0 <= outcome.ask <= 1.0


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_pinned_market_fetched_when_dropped_from_top_n(respx_mock):
    # 实时性 F-2：pinned 市场即使不在列表里，也按 ID 单独抓取并合入。
    # 列表只返回 id=1；pinned 含 "99"（不在列表）→ 应额外按 /v1/markets/99 抓取。
    respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(200, json=_markets_page([_market_raw(market_id=1)], cursor=None))
    )
    respx_mock.get(path__regex=r"^/v1/markets/99$").mock(
        return_value=httpx.Response(200, json={"success": True, "data": _market_raw(market_id=99, question="Pinned market")})
    )
    respx_mock.get(path__regex=r"^/v1/markets/[^/]+/orderbook$").mock(
        return_value=httpx.Response(200, json=_DEFAULT_ORDERBOOK)
    )
    adapter = PredictFunAdapter(base_url=BASE_URL)
    adapter.pinned_ids = {"99"}
    markets = await adapter.fetch_markets()
    ids = {m.market_id for m in markets}
    assert "1" in ids and "99" in ids  # 列表市场 + pinned 市场都在


@respx.mock(base_url=BASE_URL, assert_all_called=False)
async def test_pinned_market_not_double_fetched_when_present(respx_mock):
    # pinned 市场已在列表里时，不重复按 ID 抓取（present 检查）。
    calls = {"single": 0}
    def _single(request):
        calls["single"] += 1
        return httpx.Response(200, json={"success": True, "data": _market_raw(market_id=1)})
    respx_mock.get(path__regex=r"^/v1/markets$").mock(
        return_value=httpx.Response(200, json=_markets_page([_market_raw(market_id=1)], cursor=None))
    )
    respx_mock.get(path__regex=r"^/v1/markets/1$").mock(side_effect=_single)
    respx_mock.get(path__regex=r"^/v1/markets/[^/]+/orderbook$").mock(
        return_value=httpx.Response(200, json=_DEFAULT_ORDERBOOK)
    )
    adapter = PredictFunAdapter(base_url=BASE_URL)
    adapter.pinned_ids = {"1"}  # 已在列表
    await adapter.fetch_markets()
    assert calls["single"] == 0  # 不重复单抓
