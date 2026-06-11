"""In-process stores for canonical markets and arbitrage opportunities.

Phase One operates on a live snapshot held in memory. Both stores sit behind
small interfaces so the in-memory implementations can later be swapped for
Redis/Postgres without touching callers (design: "In-process store").

- ``MarketStore`` keeps the current ``CanonicalMarket`` records keyed by
  ``(platform, market_id)`` and backs ranking, matching, and the read API
  (Req 7.4).
- ``OpportunityStore`` keeps current ``ArbitrageOpportunity`` records and lists
  them sorted by ``net_profit_margin`` descending (Req 6.1, Property 10).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

from scanner.models import ArbitrageOpportunity, CanonicalMarket

MarketKey = Tuple[str, str]


@runtime_checkable
class MarketStore(Protocol):
    """Snapshot of current canonical markets keyed by (platform, market_id).

    Interface-backed so the Phase-One in-memory implementation can be replaced
    by a durable store later without changing callers (Req 7.4).
    """

    def upsert(self, markets: Iterable[CanonicalMarket]) -> None:
        """Insert or replace the given markets by their (platform, market_id)."""
        ...

    def get(self, platform: str, market_id: str) -> Optional[CanonicalMarket]:
        """Return the market for the key, or None if absent."""
        ...

    def list_all(self) -> List[CanonicalMarket]:
        """Return every stored market."""
        ...

    def list_by_platform(self, platform: str) -> List[CanonicalMarket]:
        """Return every stored market for a single platform."""
        ...

    def replace_platform(
        self, platform: str, markets: Iterable[CanonicalMarket]
    ) -> None:
        """Replace all markets for ``platform`` with the given set.

        Upserts the provided markets and removes any previously-stored market of
        the same platform that is absent from the new set, so markets that an
        adapter stops returning do not accumulate as stale ghosts.
        """
        ...


class InMemoryMarketStore:
    """Dict-based ``MarketStore`` keyed by ``(platform, market_id)``."""

    def __init__(self) -> None:
        self._markets: Dict[MarketKey, CanonicalMarket] = {}

    @staticmethod
    def _key(market: CanonicalMarket) -> MarketKey:
        return (market.platform, market.market_id)

    def upsert(self, markets: Iterable[CanonicalMarket]) -> None:
        for market in markets:
            self._markets[self._key(market)] = market

    def replace_platform(
        self, platform: str, markets: Iterable[CanonicalMarket]
    ) -> None:
        incoming = {self._key(m): m for m in markets if m.platform == platform}
        # 删除该平台不再出现的旧市场（消失的市场不应作为陈旧幽灵累积）。
        for key in [k for k in self._markets if k[0] == platform and k not in incoming]:
            del self._markets[key]
        self._markets.update(incoming)

    def get(self, platform: str, market_id: str) -> Optional[CanonicalMarket]:
        return self._markets.get((platform, market_id))

    def list_all(self) -> List[CanonicalMarket]:
        return list(self._markets.values())

    def list_by_platform(self, platform: str) -> List[CanonicalMarket]:
        return [m for m in self._markets.values() if m.platform == platform]

    def __len__(self) -> int:
        return len(self._markets)


class OpportunityStore:
    """In-memory store of current arbitrage opportunities keyed by group_id.

    ``list_sorted`` returns opportunities ordered by ``net_profit_margin``
    descending (Req 6.1, Property 10). Invalidated opportunities are removed by
    group id (Req 6.4).
    """

    def __init__(self) -> None:
        self._opportunities: Dict[str, ArbitrageOpportunity] = {}

    def upsert(self, opportunity: ArbitrageOpportunity) -> None:
        """Insert or replace an opportunity by its group_id."""
        self._opportunities[opportunity.group_id] = opportunity

    def get(self, group_id: str) -> Optional[ArbitrageOpportunity]:
        return self._opportunities.get(group_id)

    def remove(self, group_id: str) -> Optional[ArbitrageOpportunity]:
        """Remove and return the opportunity for ``group_id`` if present."""
        return self._opportunities.pop(group_id, None)

    def list_all(self) -> List[ArbitrageOpportunity]:
        return list(self._opportunities.values())

    def list_sorted(self) -> List[ArbitrageOpportunity]:
        """Opportunities sorted by ``net_profit_margin`` descending (Req 6.1)."""
        return sorted(
            self._opportunities.values(),
            key=lambda opp: opp.net_profit_margin,
            reverse=True,
        )

    def __len__(self) -> int:
        return len(self._opportunities)
