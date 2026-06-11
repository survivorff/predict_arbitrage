"""Platform adapter layer for the Prediction Market Arbitrage Scanner.

Each adapter connects to one external platform, fetches market and price data,
and normalizes it into :class:`scanner.models.CanonicalMarket` records. The
generic ingestion, matching, and arbitrage core depends only on the
``PlatformAdapter`` Protocol defined in :mod:`scanner.adapters.base`, so adding a
new platform requires no change to the core (Req 7.2).
"""

from __future__ import annotations

from scanner.adapters.base import AdapterError, PlatformAdapter

__all__ = ["AdapterError", "PlatformAdapter"]
