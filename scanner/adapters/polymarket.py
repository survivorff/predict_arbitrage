"""Polymarket platform adapter.

Discovers active markets via the Gamma API
(``gamma-api.polymarket.com/markets?active=true&closed=false``) and reads
top-of-book prices/liquidity from the public CLOB API
(``clob.polymarket.com/book?token_id=...``), normalizing both into
``CanonicalMarket`` records.

Normalization notes (Req 2.1-2.5):

- Polymarket prices are already implied probabilities in [0, 1] (Req 2.2), so no
  unit conversion is needed.
- ``volume``/``liquidity`` from Gamma are already expressed in USD (Req 2.3) and
  are mapped to ``volume_usd``/``liquidity_usd``.
- ``fee_rate`` is taken from a :class:`~scanner.fees.FlatFeeModel` (Polymarket
  charges no maker/taker fee on Phase One markets, so the default is 0.0)
  (Req 2.5).
- Best bid/ask per outcome come from the CLOB order book and feed the arbitrage
  engine's spread cost.
- Any field absent from the source is left ``None`` and recorded in
  ``field_status``/``unavailable_reasons`` as ``UNAVAILABLE`` (Req 2.4).

Validates: Property 1 (price bounds), Property 2 (non-negative magnitudes).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.fees import FlatFeeModel
from scanner.models import CanonicalMarket, FieldStatus, Outcome

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"

# Polymarket 主类目 → Gamma tag_id（用 /tags/slug/<slug> 反查得到，按真实成交量排序）。
# 用于「按类目优先抓取最火热市场」：不传类目则按全站成交量降序翻页。
CATEGORY_TAG_IDS: Dict[str, int] = {
    "politics": 2,        # 政治（成交量最高）
    "elections": 144,     # 选举
    "sports": 1,          # 体育
    "geopolitics": 100265,  # 地缘政治
    "economy": 100328,    # 经济
    "culture": 596,       # 文化
    "crypto": 21,         # 加密
    "tech": 1401,         # 科技
    "business": 107,      # 商业
    "science": 74,        # 科学
}


class PolymarketAdapter:
    """Concrete :class:`PlatformAdapter` for Polymarket (Req 1.1, 1.2, 7.2)."""

    name = "polymarket"

    def __init__(
        self,
        *,
        fee_model: Optional[FlatFeeModel] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        gamma_base_url: str = GAMMA_BASE_URL,
        clob_base_url: str = CLOB_BASE_URL,
        timeout: float = 30.0,
        page_size: int = 100,
        max_markets: int = 200,
        categories: Optional[List[str]] = None,
    ) -> None:
        self._fee_model = fee_model if fee_model is not None else FlatFeeModel(0.0)
        self._client = http_client
        self._owns_client = http_client is None
        self._gamma_base_url = gamma_base_url.rstrip("/")
        self._clob_base_url = clob_base_url.rstrip("/")
        self._timeout = timeout
        # 分页拉取：Gamma 默认每页仅 ~20 条，需用 limit/offset 翻页才能覆盖更多市场，
        # 进而与 predict.fun 产生更多跨平台匹配。按成交量降序优先取最活跃的市场，
        # 并限量 max_markets 控制 CLOB 订单簿读取规模、保证刷新周期内完成。
        self._page_size = max(1, page_size)
        self._max_markets = max(1, max_markets)
        # 按类目优先抓取（数据驱动）：传入类目 slug 列表（见 CATEGORY_TAG_IDS）则
        # 逐类目按成交量降序抓取（优先火热类目，如 politics/sports/elections），
        # 在 max_markets 限额内分配；不传则按全站成交量降序翻页。未知 slug 被忽略。
        self._categories = [
            c for c in (categories or []) if c in CATEGORY_TAG_IDS
        ]
        # Cache of market_id -> [token_id per outcome index] for refresh reads.
        self._token_cache: Dict[str, List[Optional[str]]] = {}

    # -- client lifecycle --------------------------------------------------- #
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the underlying client if this adapter created it."""
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
                import asyncio as _asyncio

                _asyncio.ensure_future(client.aclose())
            except Exception:  # noqa: BLE001
                pass

    @property
    def fee_rate(self) -> float:
        """The flat fee rate applied to Polymarket markets (Req 2.5)."""
        return self._fee_model.rate

    # -- public API --------------------------------------------------------- #
    async def fetch_markets(self) -> List[CanonicalMarket]:
        """Fetch active, open markets from Gamma (paginated) and normalize them.

        Pages through Gamma with ``limit``/``offset`` ordered by volume descending
        so the most active markets are covered first, capped at ``max_markets``.

        Raises:
            AdapterError: on transport failure or an unparseable Gamma response.
        """
        raw_markets = await self._list_raw_markets()
        retrieved_at = datetime.now(timezone.utc)
        markets = await asyncio.gather(
            *(self._normalize_market(raw, retrieved_at) for raw in raw_markets)
        )
        return [m for m in markets if m is not None]

    async def _list_raw_markets(self) -> List[Dict[str, Any]]:
        """Discover markets up to ``max_markets``.

        When ``categories`` is set, fetch per-category by ``tag_id`` (volume-desc),
        distributing the budget across categories and de-duplicating by id so the
        hottest markets of the prioritized categories are covered. Otherwise page
        the whole site by volume descending.
        """
        if not self._categories:
            return await self._page_markets(self._max_markets, tag_id=None)

        # 按类目分配预算：均分到各类目，余数给靠前（更火热）的类目。
        n = len(self._categories)
        base = max(1, self._max_markets // n)
        collected: List[Dict[str, Any]] = []
        seen: set = set()
        for cat in self._categories:
            remaining = self._max_markets - len(collected)
            if remaining <= 0:
                break
            quota = min(remaining, max(base, 1))
            page = await self._page_markets(quota, tag_id=CATEGORY_TAG_IDS[cat])
            for m in page:
                mid = m.get("conditionId") or m.get("id") or m.get("slug")
                if mid is not None and mid in seen:
                    continue
                if mid is not None:
                    seen.add(mid)
                # 标注该市场来自哪个类目（供规范化时写入 CanonicalMarket.category）。
                m.setdefault("_scanner_category", cat)
                collected.append(m)
        return collected[: self._max_markets]

    async def _page_markets(
        self, budget: int, *, tag_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Page through Gamma markets (volume-desc) up to ``budget`` records.

        ``tag_id`` filters to a single category; ``None`` queries the whole site.
        The first page failing raises ``AdapterError``; later-page failures
        degrade to whatever was collected.
        """
        client = self._get_client()
        url = f"{self._gamma_base_url}/markets"
        collected: List[Dict[str, Any]] = []
        offset = 0
        max_pages = (budget // self._page_size) + 2
        for _ in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": str(self._page_size),
                "offset": str(offset),
                # 按成交量降序，优先覆盖最活跃市场（套利价值最高）。
                "order": "volumeNum",
                "ascending": "false",
            }
            if tag_id is not None:
                params["tag_id"] = str(tag_id)
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                if not collected:
                    self._reset_client()
                    raise AdapterError(
                        f"failed to fetch Polymarket markets: {exc!r}", adapter=self.name
                    ) from exc
                self._reset_client()
                break
            except json.JSONDecodeError as exc:
                if not collected:
                    raise AdapterError(
                        f"invalid JSON from Polymarket Gamma API: {exc}",
                        adapter=self.name,
                    ) from exc
                break
            page = self._extract_market_list(payload)
            if not page:
                break
            collected.extend(page)
            if len(page) < self._page_size or len(collected) >= budget:
                break
            offset += self._page_size
        return collected[:budget]

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        """Refresh top-of-book prices/liquidity for the given markets.

        Re-reads the CLOB order book for each cached outcome token and restamps
        ``retrieved_at``. Markets without cached token ids are returned with a
        fresh timestamp only.

        Raises:
            AdapterError: on transport failure reading the CLOB API.
        """
        refreshed: List[CanonicalMarket] = []
        retrieved_at = datetime.now(timezone.utc)
        for market in markets:
            token_ids = self._token_cache.get(market.market_id)
            if not token_ids:
                refreshed.append(
                    market.model_copy(update={"retrieved_at": retrieved_at})
                )
                continue
            new_outcomes: List[Outcome] = []
            for index, outcome in enumerate(market.outcomes):
                token_id = token_ids[index] if index < len(token_ids) else None
                book = await self._fetch_book(token_id) if token_id else None
                new_outcomes.append(self._apply_book(outcome, book))
            refreshed.append(
                market.model_copy(
                    update={"outcomes": new_outcomes, "retrieved_at": retrieved_at}
                )
            )
        return refreshed

    # -- normalization ------------------------------------------------------ #
    @staticmethod
    def _extract_market_list(payload: Any) -> List[Dict[str, Any]]:
        """Gamma returns either a bare list or ``{"data": [...]}``."""
        if isinstance(payload, list):
            return [m for m in payload if isinstance(m, dict)]
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [m for m in data if isinstance(m, dict)]
        raise AdapterError(
            "unexpected Polymarket Gamma payload shape", adapter="polymarket"
        )

    async def _normalize_market(
        self, raw: Dict[str, Any], retrieved_at: datetime
    ) -> Optional[CanonicalMarket]:
        market_id = self._first_str(raw, "conditionId", "id", "slug")
        if market_id is None:
            # Without a stable identifier the record cannot be stored/matched.
            return None
        title = self._first_str(raw, "question", "title") or market_id

        outcome_names = self._parse_json_list(raw.get("outcomes"))
        prices = self._parse_json_list(raw.get("outcomePrices"))
        token_ids = self._parse_json_list(raw.get("clobTokenIds"))

        field_status: Dict[str, FieldStatus] = {}
        unavailable_reasons: Dict[str, str] = {}

        outcomes: List[Outcome] = []
        cached_tokens: List[Optional[str]] = []
        for index, name in enumerate(outcome_names):
            price = self._safe_float(prices[index]) if index < len(prices) else None
            token_id = (
                str(token_ids[index])
                if index < len(token_ids) and token_ids[index] is not None
                else None
            )
            cached_tokens.append(token_id)
            book = await self._fetch_book(token_id) if token_id else None
            bid, ask, liquidity = self._book_levels(book)
            if price is None:
                # Fall back to the mid of the book when Gamma omits a price.
                price = self._mid(bid, ask)
            if price is None:
                # No usable price for this outcome; flag and skip the outcome.
                field_status[f"outcomes[{index}].price"] = FieldStatus.UNAVAILABLE
                unavailable_reasons[f"outcomes[{index}].price"] = (
                    "no price in Gamma outcomePrices and empty CLOB order book"
                )
                continue
            outcomes.append(
                Outcome(
                    name=str(name),
                    price=self._clamp_unit(price),
                    bid=self._clamp_unit(bid) if bid is not None else None,
                    ask=self._clamp_unit(ask) if ask is not None else None,
                    available_liquidity_usd=liquidity,
                )
            )

        if not outcomes:
            field_status["outcomes"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["outcomes"] = "no usable outcomes in source data"

        volume_usd = self._safe_float(
            self._first_present(raw, "volumeNum", "volume")
        )
        if volume_usd is None:
            field_status["volume_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["volume_usd"] = "missing from Gamma market"
        else:
            field_status["volume_usd"] = FieldStatus.OK

        liquidity_usd = self._safe_float(
            self._first_present(raw, "liquidityNum", "liquidity")
        )
        if liquidity_usd is None:
            field_status["liquidity_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["liquidity_usd"] = "missing from Gamma market"
        else:
            field_status["liquidity_usd"] = FieldStatus.OK

        self._token_cache[market_id] = cached_tokens

        # 类目：优先用抓取时标注的来源类目；否则尝试从 event tags 推断已知主类目。
        category = raw.get("_scanner_category")
        if category is None:
            category = self._infer_category(raw)

        # 结算日期（Tier-0 红线 C）：Gamma 提供 ``endDate``（ISO 日期时间）/``endDateIso``
        # （仅日期）。解析为 UTC datetime 写入 resolution_date，供匹配引擎做日期硬 veto，
        # 区分「标题相同、结算窗口不同」的子市场。缺失/不可解析时为 None。
        resolution_date = self._parse_end_date(raw)
        if resolution_date is None:
            field_status["resolution_date"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["resolution_date"] = "missing/unparseable endDate"

        return CanonicalMarket(
            platform=self.name,
            market_id=market_id,
            title=title,
            outcomes=outcomes,
            volume_usd=volume_usd,
            liquidity_usd=liquidity_usd,
            fee_rate=self._fee_model.rate,
            retrieved_at=retrieved_at,
            field_status=field_status,
            unavailable_reasons=unavailable_reasons,
            category=category,
            resolution_date=resolution_date,
        )

    @staticmethod
    def _parse_end_date(raw: Dict[str, Any]) -> Optional[datetime]:
        """从 Gamma 市场解析结算日期为 UTC datetime。

        优先用 ``endDate``（含时间，如 ``2026-12-31T00:00:00Z``），回退到 ``endDateIso``
        （仅日期，如 ``2026-12-31``）。两者皆无/不可解析时返回 None。
        """
        for key in ("endDate", "endDateIso"):
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                continue
            text = value.strip()
            # 兼容末尾 'Z'（UTC）：Python <3.11 的 fromisoformat 不识别 'Z'。
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                # 仅日期且无分隔符等异常情形：尝试只取日期部分。
                try:
                    dt = datetime.fromisoformat(text[:10])
                except ValueError:
                    continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        return None

    @staticmethod
    def _infer_category(raw: Dict[str, Any]) -> Optional[str]:
        """从 event tags 推断已知主类目（slug 命中 CATEGORY_TAG_IDS 即采用）。"""
        events = raw.get("events")
        if not isinstance(events, list):
            return None
        for ev in events:
            if not isinstance(ev, dict):
                continue
            tags = ev.get("tags")
            if not isinstance(tags, list):
                continue
            for tag in tags:
                slug = tag.get("slug") if isinstance(tag, dict) else None
                if slug in CATEGORY_TAG_IDS:
                    return slug
        return None

    async def _fetch_book(self, token_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token_id:
            return None
        client = self._get_client()
        url = f"{self._clob_base_url}/book"
        try:
            response = await client.get(url, params={"token_id": token_id})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"failed to read Polymarket CLOB book for {token_id}: {exc}",
                adapter=self.name,
            ) from exc
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"invalid JSON from Polymarket CLOB book for {token_id}: {exc}",
                adapter=self.name,
            ) from exc

    def _apply_book(
        self, outcome: Outcome, book: Optional[Dict[str, Any]]
    ) -> Outcome:
        bid, ask, liquidity = self._book_levels(book)
        price = self._mid(bid, ask)
        if price is None:
            price = outcome.price
        return Outcome(
            name=outcome.name,
            price=self._clamp_unit(price),
            bid=self._clamp_unit(bid) if bid is not None else None,
            ask=self._clamp_unit(ask) if ask is not None else None,
            available_liquidity_usd=(
                liquidity
                if liquidity is not None
                else outcome.available_liquidity_usd
            ),
        )

    # -- order book helpers ------------------------------------------------- #
    def _book_levels(
        self, book: Optional[Dict[str, Any]]
    ) -> "tuple[Optional[float], Optional[float], Optional[float]]":
        """Return (best_bid, best_ask, ask_side_liquidity_usd) from a book."""
        if not isinstance(book, dict):
            return None, None, None
        bids = self._parse_levels(book.get("bids"))
        asks = self._parse_levels(book.get("asks"))
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        liquidity_usd: Optional[float] = None
        if best_ask is not None:
            # USD available to buy at the best ask = size * price.
            size_at_ask = next(
                (s for p, s in asks if p == best_ask), None
            )
            if size_at_ask is not None:
                liquidity_usd = round(size_at_ask * best_ask, 6)
        return best_bid, best_ask, liquidity_usd

    @staticmethod
    def _parse_levels(levels: Any) -> "List[tuple[float, float]]":
        parsed: List["tuple[float, float]"] = []
        if not isinstance(levels, list):
            return parsed
        for level in levels:
            price: Optional[float] = None
            size: Optional[float] = None
            if isinstance(level, dict):
                price = PolymarketAdapter._safe_float(level.get("price"))
                size = PolymarketAdapter._safe_float(level.get("size"))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = PolymarketAdapter._safe_float(level[0])
                size = PolymarketAdapter._safe_float(level[1])
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

    # -- small parsing utilities ------------------------------------------- #
    @staticmethod
    def _parse_json_list(value: Any) -> List[Any]:
        """Gamma encodes list fields as JSON strings (e.g. '["Yes","No"]')."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return []
            return parsed if isinstance(parsed, list) else []
        return []

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
        """Defensively clamp a probability into [0, 1] for Property 1."""
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    @staticmethod
    def _first_str(raw: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
        return None

    @staticmethod
    def _first_present(raw: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in raw and raw[key] is not None:
                return raw[key]
        return None


# Static type check: PolymarketAdapter conforms to the PlatformAdapter Protocol.
_: PlatformAdapter = PolymarketAdapter()


__all__ = ["PolymarketAdapter", "GAMMA_BASE_URL", "CLOB_BASE_URL"]
