"""Platform fee models.

The arbitrage engine recomputes exact fees per platform rather than relying on a
single flat ``fee_rate``, because some platforms (notably Kalshi) charge a
price-dependent fee. The ``FeeModel`` Protocol keeps that logic out of the
generic engine while still satisfying Req 5.2 (net margin after fees).

Fee models implemented here:

- ``FlatFeeModel``  — a fixed rate applied to notional (Polymarket-style, often
  ``0.0``).
- ``KalshiFeeModel`` — Kalshi's trading fee, ``0.07 * price * (1 - price)`` per
  contract, rounded up to the next cent per fill.

Validates: Property 8 (fee accounting feeds net_profit_margin).
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class FeeModel(Protocol):
    """Computes the fee, in USD, for buying contracts of an outcome."""

    def fee_for(self, price: float, contracts: float) -> float:
        """Return the fee in USD for buying ``contracts`` of an outcome at ``price``.

        ``price`` is the per-contract cost expressed as an implied probability
        in [0, 1]; one contract pays out $1 if the outcome resolves true.
        """
        ...


class FlatFeeModel:
    """A fixed-rate fee applied to traded notional (Polymarket-style).

    The fee is ``rate * price * contracts``. Notional for ``contracts`` bought
    at ``price`` is ``price * contracts`` (each contract costs ``price`` dollars
    and pays $1). With ``rate=0.0`` (the Phase One Polymarket default) the fee
    is always zero.
    """

    def __init__(self, rate: float = 0.0) -> None:
        if rate < 0:
            raise ValueError("fee rate must be >= 0")
        self.rate = rate

    def fee_for(self, price: float, contracts: float) -> float:
        if contracts < 0:
            raise ValueError("contracts must be >= 0")
        return self.rate * price * contracts


class KalshiFeeModel:
    """Kalshi's price-dependent trading fee.

    The fee for a fill is ``0.07 * contracts * price * (1 - price)`` rounded up
    to the next whole cent. The fee is maximized at ``price = 0.5`` and falls to
    zero at the price extremes (``price = 0`` or ``price = 1``).

    The ``coefficient`` is configurable to accommodate the higher-fee schedule
    on some Kalshi market series, but defaults to the standard ``0.07``.
    """

    def __init__(self, coefficient: float = 0.07) -> None:
        if coefficient < 0:
            raise ValueError("fee coefficient must be >= 0")
        self.coefficient = coefficient

    def fee_for(self, price: float, contracts: float) -> float:
        if contracts < 0:
            raise ValueError("contracts must be >= 0")
        raw = self.coefficient * contracts * price * (1.0 - price)
        # Round up to the next cent per Kalshi's published fee schedule.
        cents = math.ceil(round(raw * 100, 9))
        return cents / 100.0


__all__ = ["FeeModel", "FlatFeeModel", "KalshiFeeModel"]
