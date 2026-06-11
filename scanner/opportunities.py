"""Opportunity listing, threshold filtering, and reconciliation (Req 5.5, 6.1, 6.4).

The :class:`ArbitrageEngine` records an opportunity for *every* evaluated group,
including those with a ``net_profit_margin`` of 0 or below (Req 5.3). This module
is the wiring layer that reconciles that raw engine output into the
:class:`~scanner.store.OpportunityStore` so that what the User ultimately sees
upholds the listing invariants:

- Opportunities below the User's ``min_net_profit_margin`` threshold are not
  listed (Req 5.5).
- Opportunities whose margin has dropped to 0 or below are removed; the listed
  set never contains an opportunity with ``net_profit_margin <= 0``
  (Req 6.4, Property 10).
- The listing is sorted by ``net_profit_margin`` descending (Req 6.1,
  Property 10).

Each evaluation cycle is a full reconciliation: opportunities that are no longer
valid (margin dropped below threshold or to <= 0, or whose group disappeared
from the engine output) are removed from the store, while new and updated ones
are upserted. This keeps the store an accurate snapshot of the *currently
listable* opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

from scanner.models import ArbitrageOpportunity
from scanner.store import OpportunityStore


@dataclass
class OpportunityService:
    """Reconciles ArbitrageEngine output into an ``OpportunityStore``.

    Args:
        store: The opportunity store to keep in sync. Defaults to a fresh
            in-memory ``OpportunityStore``.
        min_net_profit_margin: The User's minimum net profit margin threshold
            (Req 5.5). Opportunities with a margin below this value are excluded
            from the listing. Independently of this threshold, opportunities with
            a margin of 0 or below are always excluded (Req 6.4, Property 10).
    """

    store: OpportunityStore = field(default_factory=OpportunityStore)
    min_net_profit_margin: float = 0.0

    def reconcile(
        self, opportunities: Iterable[ArbitrageOpportunity]
    ) -> List[ArbitrageOpportunity]:
        """Reconcile a full cycle of engine output into the store.

        Listable opportunities (margin > 0 and >= the threshold) are upserted;
        every other opportunity in the cycle is removed, as is any previously
        stored opportunity whose group is absent from this cycle. Returns the
        resulting filtered, sorted listing.
        """
        opportunities = list(opportunities)
        present_group_ids = {opp.group_id for opp in opportunities}

        # Drop stored opportunities whose group disappeared from this cycle.
        for stored in self.store.list_all():
            if stored.group_id not in present_group_ids:
                self.store.remove(stored.group_id)

        # Upsert listable opportunities; remove the rest (e.g. margin dropped
        # below threshold or to <= 0 since the last cycle).
        for opp in opportunities:
            if self._is_listable(opp):
                self.store.upsert(opp)
            else:
                self.store.remove(opp.group_id)

        return self.list()

    def list(self) -> List[ArbitrageOpportunity]:
        """Return the filtered opportunities sorted by net margin descending.

        Filtering is reapplied defensively so the listing upholds Property 10
        (no ``net_profit_margin <= 0``, sorted descending) regardless of how the
        underlying store was populated.
        """
        return sorted(
            (opp for opp in self.store.list_all() if self._is_listable(opp)),
            key=lambda opp: opp.net_profit_margin,
            reverse=True,
        )

    def _is_listable(self, opportunity: ArbitrageOpportunity) -> bool:
        """True when an opportunity belongs in the User-facing listing.

        An opportunity is listable only when its margin is strictly positive
        (Req 6.4, Property 10) and at or above the configured threshold
        (Req 5.5).
        """
        if opportunity.net_profit_margin <= 0:
            return False
        if opportunity.net_profit_margin < self.min_net_profit_margin:
            return False
        return True


__all__ = ["OpportunityService"]
