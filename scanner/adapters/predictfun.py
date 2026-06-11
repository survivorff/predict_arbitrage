"""predict.fun 平台适配器（Phase Three）。

predict.fun 是建在 BNB Chain 上的链上预测市场：off-chain CLOB 订单簿 + 链上结算，
二元 YES/NO、价格 0–1，与 Polymarket 架构高度相似。本适配器实现**只读**行情接入
（`PlatformAdapter`），下单（链上签名）属于第二步的执行适配器，不在此模块。

行情来源（Beta API，base url 见 ``DEFAULT_BASE_URL`` / ``TESTNET_BASE_URL``）：
- ``GET /v1/markets?status=OPEN`` —— 市场列表（游标分页，``{success, cursor, data:[Market]}``）。
- ``GET /v1/markets/{id}/orderbook`` —— 订单簿（``{success, data:{asks:[[price,size]], bids:[[price,size]]}}``，价格已 0–1）。

规范化要点（Req 2.1–2.5）：
- 价格已是隐含概率 0–1，无需换算（防御性 clamp）。
- 二元市场产出 YES / NO 两个 `Outcome`；bid/ask 取自订单簿最优买/卖。
- ``feeRateBps`` 基点 → 费率（÷10000）。
- 缺失字段标记 ``UNAVAILABLE`` 并记原因。
- **跨平台关联**：市场自带的 ``polymarketConditionIds`` / ``kalshiMarketTicker`` 写入
  `CanonicalMarket.cross_refs`，供匹配引擎作「金标准」关联（比纯标题语义可靠）。

鉴权：行情读取可选携带 ``x-api-key``（从构造参数或 ``PREDICTFUN_API_KEY`` 环境变量），
缺失时仍尝试公开读取（优雅降级）。下单所需的钱包签名/JWT 不在只读适配器内。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.fees import FlatFeeModel
from scanner.models import CanonicalMarket, FieldStatus, Outcome

DEFAULT_BASE_URL = "https://api.predict.fun"
TESTNET_BASE_URL = "https://api-testnet.predict.fun"


class PredictFunAdapter:
    """只读行情适配器 for predict.fun（Req 1.1, 1.2, 7.2）。"""

    name = "predictfun"

    def __init__(
        self,
        *,
        fee_model: Optional[FlatFeeModel] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        base_url: str = DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        max_pages: int = 20,
        max_concurrency: int = 20,
        max_markets: int = 150,
    ) -> None:
        self._fee_model = fee_model if fee_model is not None else FlatFeeModel(0.0)
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._api_key = (
            api_key if api_key is not None else os.environ.get("PREDICTFUN_API_KEY")
        )
        self._timeout = timeout
        self._max_pages = max_pages
        # 单次发现的市场数量上限：predict.fun 市场数可达数百，每个市场需读一次订单簿，
        # 全量抓取会超过刷新周期。优先按 24h 成交量降序取最活跃的 N 个（套利价值最高），
        # 使每个刷新周期能在限定时间内完成，达成「实时」。
        self._max_markets = max_markets
        # 限制并发订单簿请求数，避免一次性轰炸平台 API 触发限流。
        # 懒初始化：Semaphore 绑定创建它的事件循环，必须在实际运行的循环内创建，
        # 否则跨事件循环使用会报 "attached to a different loop"。
        self._max_concurrency = max(1, max_concurrency)
        self._semaphore: Optional[asyncio.Semaphore] = None
        # 已匹配市场「固定抓取」名单（实时性 · F-2）：这些 market_id 即使因成交量下滑
        # 跌出 top-N，也会被单独按 ID 抓取并合入，避免已建立的跨平台套利对忽隐忽现。
        # 由 app 在每个流水线周期后用「当前匹配组里的 predict.fun 成员」更新。
        self.pinned_ids: "set[str]" = set()

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    # -- client lifecycle --------------------------------------------------- #
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _reset_client(self) -> None:
        """丢弃当前 client，使下次请求重建。

        长时间运行（尤其机器休眠/网络中断）后，复用的 httpx.AsyncClient 连接池里的
        连接会失效，导致后续请求持续传输失败、熔断永不恢复。传输层错误时重置 client，
        让退避重试与熔断半开探测能用全新连接自愈。
        """
        if self._client is not None and self._owns_client:
            client = self._client
            self._client = None
            try:
                # 尽力关闭旧 client（释放套接字）；失败忽略。
                import asyncio as _asyncio

                _asyncio.ensure_future(client.aclose())
            except Exception:  # noqa: BLE001
                pass

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    # -- public API --------------------------------------------------------- #
    async def fetch_markets(self) -> List[CanonicalMarket]:
        """拉取 OPEN 状态的市场并规范化。

        Raises:
            AdapterError: 传输失败或响应不可解析。
        """
        raw_markets = await self._list_raw_markets()
        retrieved_at = datetime.now(timezone.utc)
        markets = await asyncio.gather(
            *(self._normalize_market(raw, retrieved_at) for raw in raw_markets)
        )
        return [m for m in markets if m is not None]

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        """刷新给定市场的订单簿价格/流动性，重打时间戳（并发，受信号量限流）。"""
        retrieved_at = datetime.now(timezone.utc)
        return list(
            await asyncio.gather(
                *(self._refresh_one(m, retrieved_at) for m in markets)
            )
        )

    async def _refresh_one(
        self, market: CanonicalMarket, retrieved_at: datetime
    ) -> CanonicalMarket:
        book = await self._fetch_orderbook(market.market_id)
        if book is None:
            return market.model_copy(update={"retrieved_at": retrieved_at})
        yes_bid, yes_ask, ask_liq = self._book_levels(book)
        new_outcomes: List[Outcome] = []
        for outcome in market.outcomes:
            if outcome.name == "YES":
                new_outcomes.append(self._refresh_outcome(outcome, yes_bid, yes_ask, ask_liq))
            elif outcome.name == "NO":
                no_bid = 1.0 - yes_ask if yes_ask is not None else None
                no_ask = 1.0 - yes_bid if yes_bid is not None else None
                new_outcomes.append(self._refresh_outcome(outcome, no_bid, no_ask, ask_liq))
            else:
                new_outcomes.append(outcome)
        return market.model_copy(
            update={"outcomes": new_outcomes, "retrieved_at": retrieved_at}
        )

    # -- HTTP -------------------------------------------------------------- #
    async def _list_raw_markets(self) -> List[Dict[str, Any]]:
        client = self._get_client()
        collected: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(self._max_pages):
            params: Dict[str, str] = {"status": "OPEN"}
            if cursor:
                params["after"] = cursor
            else:
                # 首页起按 24h 成交量降序，优先取最活跃市场。
                params["sort"] = "VOLUME_24H_DESC"
            payload = await self._get_json(client, "/v1/markets", params)
            data = payload.get("data")
            if not isinstance(data, list):
                raise AdapterError(
                    "predict.fun markets response missing 'data' list",
                    adapter=self.name,
                )
            collected.extend(m for m in data if isinstance(m, dict))
            cursor = payload.get("cursor") or None
            # 达到数量上限或无更多页即停止（控制订单簿抓取规模以保证实时性）。
            if not cursor or not data or len(collected) >= self._max_markets:
                break
        collected = collected[: self._max_markets]
        # 固定抓取已匹配市场（F-2）：补齐跌出 top-N 的 pinned 市场，避免套利对闪烁。
        await self._merge_pinned(client, collected)
        return collected

    async def _merge_pinned(
        self, client: httpx.AsyncClient, collected: List[Dict[str, Any]]
    ) -> None:
        """把 pinned_ids 中尚未在本轮列表里的市场按 ID 单独抓取并合入。"""
        if not self.pinned_ids:
            return
        present = {str(m.get("id")) for m in collected if m.get("id") is not None}
        missing = [mid for mid in self.pinned_ids if mid not in present]
        for mid in missing:
            try:
                payload = await self._get_json(client, f"/v1/markets/{mid}", None)
            except AdapterError:
                continue  # 单个 pinned 市场抓取失败不影响整体
            data = payload.get("data")
            if isinstance(data, dict):
                collected.append(data)

    async def _fetch_orderbook(self, market_id: str) -> Optional[Dict[str, Any]]:
        client = self._get_client()
        try:
            # 信号量限制并发，避免一次性发起过多订单簿请求触发平台限流。
            async with self._get_semaphore():
                payload = await self._get_json(
                    client, f"/v1/markets/{market_id}/orderbook", None
                )
        except AdapterError:
            # 订单簿读取失败不致命：市场仍可带价格缺失被摄取。
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = await client.get(url, params=params, headers=self._headers())
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise AdapterError(
                f"predict.fun HTTP {exc.response.status_code} for {path}",
                adapter=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            # 传输层错误（连接失败/超时，常见于休眠后连接池失效）：重置 client，
            # 使下次请求用全新连接，让重试/熔断半开探测能自愈。
            self._reset_client()
            raise AdapterError(
                f"predict.fun request to {path} failed: {exc!r}", adapter=self.name
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise AdapterError(
                f"predict.fun returned malformed JSON for {path}: {exc}",
                adapter=self.name,
            ) from exc

    # -- normalization ----------------------------------------------------- #
    async def _normalize_market(
        self, raw: Dict[str, Any], retrieved_at: datetime
    ) -> Optional[CanonicalMarket]:
        market_id = raw.get("id")
        if market_id is None:
            return None
        market_id = str(market_id)
        title = raw.get("question") or raw.get("title")
        if not title:
            return None

        field_status: Dict[str, FieldStatus] = {}
        unavailable_reasons: Dict[str, str] = {}

        # 订单簿 → YES/NO 的 bid/ask。
        book = await self._fetch_orderbook(market_id)
        yes_bid, yes_ask, ask_liq = self._book_levels(book)
        yes_price = self._mid(yes_bid, yes_ask)
        if yes_price is None:
            field_status["outcomes"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["outcomes"] = "empty orderbook / no usable price"
            outcomes: List[Outcome] = []
        else:
            no_price = 1.0 - yes_price
            no_bid = 1.0 - yes_ask if yes_ask is not None else None
            no_ask = 1.0 - yes_bid if yes_bid is not None else None
            outcomes = [
                Outcome(
                    name="YES",
                    price=self._clamp_unit(yes_price),
                    bid=self._clamp_unit(yes_bid) if yes_bid is not None else None,
                    ask=self._clamp_unit(yes_ask) if yes_ask is not None else None,
                    available_liquidity_usd=ask_liq,
                ),
                Outcome(
                    name="NO",
                    price=self._clamp_unit(no_price),
                    bid=self._clamp_unit(no_bid) if no_bid is not None else None,
                    ask=self._clamp_unit(no_ask) if no_ask is not None else None,
                    available_liquidity_usd=ask_liq,
                ),
            ]

        # 费率：基点 → 比率。
        fee_rate = self._fee_rate(raw)
        field_status["fee_rate"] = FieldStatus.OK

        # 跨平台关联线索（金标准）。
        cross_refs: Dict[str, List[str]] = {}
        poly_ids = raw.get("polymarketConditionIds")
        if isinstance(poly_ids, list) and poly_ids:
            cross_refs["polymarket"] = [str(x) for x in poly_ids]
        kalshi_ticker = raw.get("kalshiMarketTicker")
        if isinstance(kalshi_ticker, str) and kalshi_ticker:
            cross_refs["kalshi"] = [kalshi_ticker]

        # 流动性：从订单簿全档深度估算（Σ 各档 price×size，买卖两侧）。这是当前 API
        # 真实可得的指标，远胜「不可用」。订单簿缺失时才标记不可用。
        liquidity_usd = self._book_depth_usd(book)
        if liquidity_usd is not None:
            field_status["liquidity_usd"] = FieldStatus.OK
        else:
            field_status["liquidity_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["liquidity_usd"] = "empty/unavailable orderbook"

        # 成交量：列表端点的 ``stats`` 当前为空、``statistics`` 端点不存在（404）。
        # 防御性地从 ``stats`` 提取（若将来 API 填充则自动生效），否则诚实标记不可用。
        volume_usd = self._extract_volume(raw)
        if volume_usd is not None:
            field_status["volume_usd"] = FieldStatus.OK
        else:
            field_status["volume_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["volume_usd"] = "not provided by markets endpoint"

        # 结算日期（Tier-0 红线 C）：predict.fun 的 markets 端点**没有结构化的结算日期
        # 字段**，但 ``description`` 文本里通常写明「...by December 31, 2026, 11:59 PM ET」
        # 之类的截止日。这里用正则从描述中解析出结算日期，供匹配引擎做日期硬 veto，区分
        # 「标题相同、结算窗口不同」的子市场。解析失败时为 None（日期维度按中性处理）。
        #
        # 诚实说明：这是从自由文本解析，非结构化字段，可能漏解析或解析偏差；但由于日期
        # veto 只会**阻止**匹配（错配→真亏钱），不会**制造**匹配（漏配→只是错过），
        # 解析偏差最坏只导致漏配，方向是安全的（符合「宁可漏不可错」原则）。
        resolution_date = self._parse_resolution_date(raw.get("description"))
        if resolution_date is None:
            field_status["resolution_date"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["resolution_date"] = (
                "no structured date field; not found in description"
            )

        return CanonicalMarket(
            platform=self.name,
            market_id=market_id,
            title=str(title),
            outcomes=outcomes,
            volume_usd=volume_usd,
            liquidity_usd=liquidity_usd,
            fee_rate=fee_rate,
            retrieved_at=retrieved_at,
            field_status=field_status,
            unavailable_reasons=unavailable_reasons,
            cross_refs=cross_refs,
            resolution_date=resolution_date,
        )

    # 月份名 → 月份号，用于从描述文本解析结算日期。
    _MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    # 「Month Day, Year」如 "December 31, 2026"。
    _DATE_MDY_RE = re.compile(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        re.IGNORECASE,
    )

    def _parse_resolution_date(self, description: Any) -> Optional[datetime]:
        """从市场描述文本解析结算日期（UTC）。

        predict.fun 无结构化结算日期字段，但描述里常见「...by December 31, 2026,
        11:59 PM ET...」这类截止日。这里匹配「Month Day, Year」模式：优先取紧跟在
        ``by`` / ``before`` / ``on or before`` 之后的日期（结算截止日的惯用措辞），
        否则退而取文本中**第一个**日期。无法解析时返回 None。

        诚实说明：自由文本解析不保证 100% 准确，但日期 veto 只阻止匹配、不制造匹配，
        故解析偏差最坏只导致漏配（安全方向）。
        """
        if not isinstance(description, str) or not description:
            return None
        text = description.strip()

        # 优先：紧跟 by/before/on or before 之后的日期（结算截止日的惯用措辞）。
        best: Optional[datetime] = None
        for m in self._DATE_MDY_RE.finditer(text):
            dt = self._mdy_to_datetime(m)
            if dt is None:
                continue
            prefix = text[max(0, m.start() - 24): m.start()].lower()
            if best is None:
                best = dt  # 兜底：第一个可解析的日期
            if re.search(r"\b(?:by|before|on or before|no later than)\s*$", prefix):
                return dt  # 命中截止日措辞，立即采用
        return best

    def _mdy_to_datetime(self, match: "re.Match") -> Optional[datetime]:
        month = self._MONTHS.get(match.group(1).lower())
        if month is None:
            return None
        try:
            day = int(match.group(2))
            year = int(match.group(3))
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def _extract_volume(self, raw: Dict[str, Any]) -> Optional[float]:
        """防御性提取成交量（USD）。

        优先从 ``stats`` 字典读取常见键名；兼容顶层字段。当前 API 这些字段为空时
        返回 None（诚实标记不可用），将来 API 填充后无需改代码即生效。
        """
        candidates = ("volume24h", "volume24H", "volumeUsd", "volume", "totalVolume")
        stats = raw.get("stats")
        sources = [stats, raw] if isinstance(stats, dict) else [raw]
        for src in sources:
            if not isinstance(src, dict):
                continue
            for key in candidates:
                val = self._safe_float(src.get(key))
                if val is not None and val >= 0:
                    return val
        return None

    def _book_depth_usd(self, book: Optional[Dict[str, Any]]) -> Optional[float]:
        """订单簿全档深度（USD）：Σ price×size，买卖两侧合计。

        反映该市场可承接的总挂单金额，是「流动性」的合理代理。订单簿缺失/为空时
        返回 None。
        """
        if not isinstance(book, dict):
            return None
        bids = self._parse_levels(book.get("bids"))
        asks = self._parse_levels(book.get("asks"))
        if not bids and not asks:
            return None
        depth = sum(p * s for p, s in bids) + sum(p * s for p, s in asks)
        return round(depth, 6)

    def _fee_rate(self, raw: Dict[str, Any]) -> float:
        bps = raw.get("feeRateBps")
        if isinstance(bps, (int, float)) and not isinstance(bps, bool):
            rate = float(bps) / 10000.0
            return self._clamp_unit(rate)
        return self._fee_model.rate

    # -- orderbook helpers -------------------------------------------------- #
    def _book_levels(
        self, book: Optional[Dict[str, Any]]
    ) -> "tuple[Optional[float], Optional[float], Optional[float]]":
        """从订单簿返回 (best_bid, best_ask, ask 侧流动性 USD)。

        predict.fun 的链下 CLOB 订单簿里会混入「穿价」的陈旧/脏挂单：本应与对手盘
        成交却仍挂在簿上的买单/卖单（例如真实卖一 0.164 时，簿里仍残留一个买价
        0.78 的买单）。朴素地取 ``best_bid=max(bids)`` 会把这个 0.78 当成真实买一，
        进而让适配器推导出 ``NO ask = 1 - 0.78 = 0.22`` 这种荒谬价，制造虚假套利。

        因此这里先**反穿价（uncross）**：正常限价簿必有 best_bid < best_ask。当
        ``max(bids) >= min(asks)`` 说明簿被穿价、含脏单，需剔除穿价的一侧。
        剔除规则以「成交量更厚的一侧」为锚——脏单通常是孤立的离群挂单，真实价格由
        密集的挂单簇决定；锚定一侧的最优价后，把对侧越过该价的挂单（本应成交却残留
        的陈旧单）丢弃，再取对侧最优价。这样无论脏单出现在买侧还是卖侧都能纠正。
        """
        if not isinstance(book, dict):
            return None, None, None
        bids = self._parse_levels(book.get("bids"))
        asks = self._parse_levels(book.get("asks"))
        best_bid, best_ask = self._uncross(bids, asks)
        ask_liq: Optional[float] = None
        if best_ask is not None:
            size_at_ask = next((s for p, s in asks if p == best_ask), None)
            if size_at_ask is not None:
                ask_liq = round(size_at_ask * best_ask, 6)
        return best_bid, best_ask, ask_liq

    @staticmethod
    def _uncross(
        bids: "List[tuple[float, float]]",
        asks: "List[tuple[float, float]]",
    ) -> "tuple[Optional[float], Optional[float]]":
        """对买/卖盘反穿价，返回干净的 (best_bid, best_ask)。

        - 任一侧为空：直接返回各自的朴素最优价（无穿价可言）。
        - 未穿价（max(bids) < min(asks)）：原样返回最优买一/卖一。
        - 穿价：以「该价位累计挂单量更大」的一侧为可信锚。锚定其最优价后，剔除对侧
          越过锚价的脏挂单（买侧剔除 >= best_ask 的买单；卖侧剔除 <= best_bid 的卖单），
          再取对侧剩余的最优价。若剔除后对侧无挂单，则对侧最优价为 None。
        """
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        # 缺任一侧或未穿价：无需纠正。
        if best_bid is None or best_ask is None or best_bid < best_ask:
            return best_bid, best_ask

        # 穿价：比较两侧最优价位上的挂单量，信赖更厚的一侧为真实盘口。
        bid_size_at_best = sum(s for p, s in bids if p == best_bid)
        ask_size_at_best = sum(s for p, s in asks if p == best_ask)

        if ask_size_at_best >= bid_size_at_best:
            # 信赖卖侧：卖一为锚，剔除越过卖一的脏买单（>= best_ask）。
            clean_bids = [p for p, _ in bids if p < best_ask]
            new_best_bid = max(clean_bids, default=None)
            return new_best_bid, best_ask
        # 信赖买侧：买一为锚，剔除越过买一的脏卖单（<= best_bid）。
        clean_asks = [p for p, _ in asks if p > best_bid]
        new_best_ask = min(clean_asks, default=None)
        return best_bid, new_best_ask

    @staticmethod
    def _parse_levels(levels: Any) -> "List[tuple[float, float]]":
        parsed: List["tuple[float, float]"] = []
        if not isinstance(levels, list):
            return parsed
        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = PredictFunAdapter._safe_float(level[0])
                size = PredictFunAdapter._safe_float(level[1])
                if price is not None and size is not None and size >= 0:
                    parsed.append((price, size))
        return parsed

    @staticmethod
    def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        if ask is not None:
            return ask
        if bid is not None:
            return bid
        return None

    def _refresh_outcome(
        self,
        outcome: Outcome,
        bid: Optional[float],
        ask: Optional[float],
        liq: Optional[float],
    ) -> Outcome:
        price = self._mid(bid, ask)
        if price is None:
            price = outcome.price
        return Outcome(
            name=outcome.name,
            price=self._clamp_unit(price),
            bid=self._clamp_unit(bid) if bid is not None else None,
            ask=self._clamp_unit(ask) if ask is not None else None,
            available_liquidity_usd=liq if liq is not None else outcome.available_liquidity_usd,
        )

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if result != result or result in (float("inf"), float("-inf")):
            return None
        return result

    @staticmethod
    def _clamp_unit(value: float) -> float:
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value


# 静态契约检查：PredictFunAdapter 满足 PlatformAdapter Protocol。
_: PlatformAdapter = PredictFunAdapter()


__all__ = ["PredictFunAdapter", "DEFAULT_BASE_URL", "TESTNET_BASE_URL"]
