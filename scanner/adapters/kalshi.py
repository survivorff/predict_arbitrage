"""KalshiAdapter — ingest and normalize Kalshi markets (Req 1.1, 1.2, 2.1–2.5).

Kalshi exposes a REST API at ``https://api.elections.kalshi.com/trade-api/v2``.
This adapter lists markets via ``GET /trade-api/v2/markets`` (paginated by
``cursor``) and normalizes each into a :class:`CanonicalMarket`.

Platform-specific quirks handled here:

- **Prices are in cents** (0–100). Every price/bid/ask is divided by 100 to
  produce an implied probability in [0, 1] (Req 2.2, Property 1).
- **Magnitudes are in cents.** Kalshi reports ``dollar_volume`` and
  ``liquidity`` as integer cents; both are divided by 100 to express USD
  (Req 2.3, Property 2).
- **Binary YES/NO structure.** Each market yields a ``YES`` and a ``NO``
  outcome. The NO price is the complement of the YES price; bid/ask for NO come
  from the ``no_bid``/``no_ask`` book fields.
- **Missing fields.** When a source field required for a canonical value is
  absent, the corresponding canonical field is left ``None`` and recorded in
  ``field_status`` as ``UNAVAILABLE`` with a reason (Req 2.4).
- **Fees** are price-dependent, so the adapter attaches a :class:`KalshiFeeModel`
  and records an effective ``fee_rate`` for reference (Req 2.5).

Auth: the trade-api markets listing is public, but an API key (read from config
or the ``KALSHI_API_KEY`` environment variable) is attached as a bearer token
when available. A missing key is handled gracefully — the adapter still lists
public markets rather than failing (Req 7.1/7.3 startup resilience).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from scanner.adapters.base import AdapterError
from scanner.fees import KalshiFeeModel
from scanner.models import CanonicalMarket, FieldStatus, Outcome

DEFAULT_BASE_URL = "https://api.elections.kalshi.com"
MARKETS_PATH = "/trade-api/v2/markets"
DEFAULT_TIMEOUT = 30.0
# Kalshi caps page size at 1000; default to a sizeable page to limit round trips.
DEFAULT_PAGE_LIMIT = 1000
# Safety cap on pagination so a misbehaving cursor cannot loop forever.
MAX_PAGES = 100


def _cents_to_probability(value: Optional[float]) -> Optional[float]:
    """Convert a Kalshi price in cents (0–100) to a probability in [0, 1]."""
    if value is None:
        return None
    prob = value / 100.0
    # Clamp tiny floating-point overshoots so the model's [0, 1] validator
    # never rejects a legitimate 0 or 100 cent price.
    if prob < 0.0:
        return 0.0
    if prob > 1.0:
        return 1.0
    return prob


def _cents_to_usd(value: Optional[float]) -> Optional[float]:
    """Convert an integer-cent monetary amount to USD."""
    if value is None:
        return None
    return value / 100.0


class KalshiAdapter:
    """Concrete :class:`PlatformAdapter` for Kalshi (Req 7.2).

    Args:
        client: optional shared ``httpx.AsyncClient``. When omitted a client is
            created per request using ``base_url``/``timeout``. Tests inject a
            client so ``respx`` can mock transport.
        base_url: Kalshi API base URL.
        api_key: explicit API key; falls back to the ``KALSHI_API_KEY`` env var.
        fee_model: the Kalshi fee model attached to each market.
        timeout: per-request timeout in seconds.
        status_filter: value for the ``status`` query param (default ``open``).
    """

    name = "kalshi"

    def __init__(
        self,
        *,
        client: Optional[httpx.AsyncClient] = None,
        base_url: str = DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        fee_model: Optional[KalshiFeeModel] = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_filter: Optional[str] = "open",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        # A missing key is fine: the markets listing is public. We simply omit
        # the auth header when no key is configured (graceful handling).
        self._api_key = api_key if api_key is not None else os.environ.get("KALSHI_API_KEY")
        self._fee_model = fee_model or KalshiFeeModel()
        self._timeout = timeout
        self._status_filter = status_filter

    # -- public adapter interface ------------------------------------------ #
    async def fetch_markets(self) -> List[CanonicalMarket]:
        """List active markets and normalize them (Req 1.1, 1.2)."""
        raw_markets = await self._list_raw_markets()
        retrieved_at = datetime.now(timezone.utc)
        return [self._normalize(m, retrieved_at) for m in raw_markets]

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        """Refresh prices/liquidity for the given markets (Req 1.3).

        Re-lists current markets and returns refreshed canonical records for the
        requested tickers, preserving the input order. Markets that are no
        longer listed are returned unchanged so callers retain last-good data.
        """
        wanted = {m.market_id for m in markets}
        raw_markets = await self._list_raw_markets()
        retrieved_at = datetime.now(timezone.utc)
        by_ticker: Dict[str, CanonicalMarket] = {}
        for raw in raw_markets:
            ticker = raw.get("ticker")
            if ticker in wanted:
                by_ticker[ticker] = self._normalize(raw, retrieved_at)
        return [by_ticker.get(m.market_id, m) for m in markets]

    # -- HTTP plumbing ------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _list_raw_markets(self) -> List[Dict]:
        """Page through ``GET /trade-api/v2/markets`` collecting raw dicts."""
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        collected: List[Dict] = []
        try:
            cursor: Optional[str] = None
            for _ in range(MAX_PAGES):
                params: Dict[str, object] = {"limit": DEFAULT_PAGE_LIMIT}
                if self._status_filter:
                    params["status"] = self._status_filter
                if cursor:
                    params["cursor"] = cursor
                payload = await self._get_json(client, MARKETS_PATH, params)
                markets = payload.get("markets")
                if not isinstance(markets, list):
                    raise AdapterError(
                        "Kalshi response missing 'markets' list", adapter=self.name
                    )
                collected.extend(markets)
                cursor = payload.get("cursor") or None
                # Kalshi returns an empty cursor (or repeats) when exhausted.
                if not cursor or not markets:
                    break
            return collected
        finally:
            if owns_client:
                await client.aclose()

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: Dict[str, object]
    ) -> Dict:
        url = f"{self._base_url}{path}"
        try:
            response = await client.get(url, params=params, headers=self._headers())
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise AdapterError(
                f"Kalshi returned HTTP {exc.response.status_code} for {path}",
                adapter=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"Kalshi request to {path} failed: {exc}", adapter=self.name
            ) from exc
        except ValueError as exc:  # JSON decode error
            raise AdapterError(
                f"Kalshi returned malformed JSON for {path}: {exc}", adapter=self.name
            ) from exc

    # -- normalization ------------------------------------------------------ #
    def _normalize(self, raw: Dict, retrieved_at: datetime) -> CanonicalMarket:
        ticker = raw.get("ticker")
        if not ticker:
            raise AdapterError(
                "Kalshi market missing 'ticker' identifier", adapter=self.name
            )

        title = raw.get("title") or raw.get("subtitle") or ticker

        field_status: Dict[str, FieldStatus] = {}
        unavailable_reasons: Dict[str, str] = {}

        # Prices are in cents; convert to probabilities (Property 1, Req 2.2).
        yes_bid = _cents_to_probability(raw.get("yes_bid"))
        yes_ask = _cents_to_probability(raw.get("yes_ask"))
        no_bid = _cents_to_probability(raw.get("no_bid"))
        no_ask = _cents_to_probability(raw.get("no_ask"))
        last_price = _cents_to_probability(raw.get("last_price"))

        yes_price = self._derive_price(last_price, yes_bid, yes_ask)
        if yes_price is None:
            # No usable price anywhere in the payload: we cannot build a
            # tradable outcome, so this market is not ingestible.
            raise AdapterError(
                f"Kalshi market {ticker} has no usable price fields",
                adapter=self.name,
            )
        no_price = 1.0 - yes_price

        outcomes = [
            Outcome(name="YES", price=yes_price, bid=yes_bid, ask=yes_ask),
            Outcome(name="NO", price=no_price, bid=no_bid, ask=no_ask),
        ]

        # Volume: Kalshi reports dollar_volume in integer cents (Req 2.3).
        volume_usd = _cents_to_usd(raw.get("dollar_volume"))
        if volume_usd is None:
            field_status["volume_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["volume_usd"] = "dollar_volume missing from source"
        else:
            field_status["volume_usd"] = FieldStatus.OK

        # Liquidity is also reported in integer cents (Req 2.3).
        liquidity_usd = _cents_to_usd(raw.get("liquidity"))
        if liquidity_usd is None:
            field_status["liquidity_usd"] = FieldStatus.UNAVAILABLE
            unavailable_reasons["liquidity_usd"] = "liquidity missing from source"
        else:
            field_status["liquidity_usd"] = FieldStatus.OK

        # Fees are price-dependent; attach an effective rate for reference while
        # the arbitrage engine recomputes exact fees from the fee model (Req 2.5).
        fee_rate = self._effective_fee_rate(yes_price)
        field_status["fee_rate"] = FieldStatus.OK

        return CanonicalMarket(
            platform=self.name,
            market_id=ticker,
            title=title,
            outcomes=outcomes,
            volume_usd=volume_usd,
            liquidity_usd=liquidity_usd,
            fee_rate=fee_rate,
            retrieved_at=retrieved_at,
            field_status=field_status,
            unavailable_reasons=unavailable_reasons,
        )

    @staticmethod
    def _derive_price(
        last_price: Optional[float], bid: Optional[float], ask: Optional[float]
    ) -> Optional[float]:
        """Pick the best available YES price as an implied probability.

        Preference: last traded price, then the bid/ask midpoint, then either
        side of the book alone.
        """
        if last_price is not None:
            return last_price
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        if ask is not None:
            return ask
        if bid is not None:
            return bid
        return None

    def _effective_fee_rate(self, price: float) -> float:
        """Effective per-contract fee as a fraction of the $1 contract notional.

        ``KalshiFeeModel`` returns a USD fee per contract; expressed as a rate
        this is simply that per-contract fee (each contract settles at $1). The
        result is clamped to [0, 1] to satisfy the canonical ``fee_rate``
        validator.
        """
        fee = self._fee_model.fee_for(price, 1.0)
        if fee < 0.0:
            return 0.0
        if fee > 1.0:
            return 1.0
        return fee


__all__ = ["KalshiAdapter"]
