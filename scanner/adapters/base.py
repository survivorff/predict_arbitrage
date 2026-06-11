"""The PlatformAdapter boundary.

Defines the single error type adapters raise (``AdapterError``) and the
``PlatformAdapter`` Protocol that every concrete adapter must satisfy. Keeping
this contract narrow lets the IngestionService treat all platforms uniformly and
centralizes failure-isolation policy (Req 1.5, 7.2).
"""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from scanner.models import CanonicalMarket


class AdapterError(Exception):
    """Raised by a PlatformAdapter when it cannot retrieve or normalize data.

    This is the single error type adapters raise. The IngestionService is the
    only place that decides isolation vs. continuation (Req 1.5), so adapters
    surface all platform-specific failures (HTTP errors, auth failures,
    malformed payloads) as an ``AdapterError`` rather than leaking transport
    exceptions to the core.
    """

    def __init__(self, message: str, *, adapter: str | None = None) -> None:
        super().__init__(message)
        self.adapter = adapter


@runtime_checkable
class PlatformAdapter(Protocol):
    """Connects to one platform and produces canonical markets (Req 2.1, 7.2).

    Concrete adapters own all platform-specific quirks (units, pagination,
    auth) and convert native data into the canonical model: prices as implied
    probabilities in [0, 1] (Req 2.2) and volume/liquidity in USD (Req 2.3).
    """

    name: str

    async def fetch_markets(self) -> List[CanonicalMarket]:
        """Fetch active markets and normalize them into canonical records.

        Raises:
            AdapterError: if the platform cannot be reached or its response
                cannot be normalized.
        """
        ...

    async def refresh_prices(
        self, markets: List[CanonicalMarket]
    ) -> List[CanonicalMarket]:
        """Refresh prices/liquidity for the given markets.

        Cheaper than a full :meth:`fetch_markets` when only price and liquidity
        need updating. Returns the refreshed canonical records.

        Raises:
            AdapterError: if the refresh cannot be completed.
        """
        ...


__all__ = ["AdapterError", "PlatformAdapter"]
