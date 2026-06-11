"""Ranking and threshold filtering for canonical markets.

The ``RankingService`` orders markets for the read API by either trading
volume or liquidity, both descending, and applies optional minimum-volume and
minimum-liquidity thresholds.

Requirements:
- Req 3.1: rank by volume descending.
- Req 3.2: rank by liquidity descending.
- Req 3.3: return markets ordered by the selected criterion.
- Req 3.4: exclude markets with volume below ``min_volume``.
- Req 3.5: exclude markets with liquidity below ``min_liquidity``.

Markets whose ranking metric is unavailable (``None``) sort after every market
that has a value, since they cannot be ranked against a known magnitude.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from scanner.models import CanonicalMarket

RankBy = Literal["volume", "liquidity"]


class RankingService:
    """Sorts and filters canonical markets by volume or liquidity."""

    def rank(
        self,
        markets: List[CanonicalMarket],
        *,
        by: RankBy,
        min_volume: Optional[float] = None,
        min_liquidity: Optional[float] = None,
    ) -> List[CanonicalMarket]:
        """Return ``markets`` filtered by thresholds and sorted descending.

        Args:
            markets: The markets to rank.
            by: The ranking metric, ``"volume"`` or ``"liquidity"``.
            min_volume: If set, drop markets whose ``volume_usd`` is unavailable
                or below this threshold (Req 3.4).
            min_liquidity: If set, drop markets whose ``liquidity_usd`` is
                unavailable or below this threshold (Req 3.5).

        Returns:
            A new list ordered by the selected metric descending (Req 3.1-3.3),
            with markets whose ranking metric is unavailable placed last.
        """
        if by not in ("volume", "liquidity"):
            raise ValueError("by must be 'volume' or 'liquidity'")

        filtered = [
            m
            for m in markets
            if self._passes_threshold(m, min_volume, min_liquidity)
        ]
        return sorted(filtered, key=lambda m: self._sort_key(m, by))

    @staticmethod
    def _metric(market: CanonicalMarket, by: RankBy) -> Optional[float]:
        return market.volume_usd if by == "volume" else market.liquidity_usd

    @staticmethod
    def _passes_threshold(
        market: CanonicalMarket,
        min_volume: Optional[float],
        min_liquidity: Optional[float],
    ) -> bool:
        # A threshold excludes markets whose value is unavailable or below it:
        # an unavailable magnitude cannot be confirmed to meet the minimum.
        if min_volume is not None:
            if market.volume_usd is None or market.volume_usd < min_volume:
                return False
        if min_liquidity is not None:
            if market.liquidity_usd is None or market.liquidity_usd < min_liquidity:
                return False
        return True

    def _sort_key(self, market: CanonicalMarket, by: RankBy):
        # Sort descending by the metric. ``sorted`` is ascending, so the first
        # element orders unavailable metrics (None) last regardless of order,
        # and the second negates the value to achieve descending order.
        value = self._metric(market, by)
        unavailable = value is None
        return (unavailable, -value if value is not None else 0.0)
