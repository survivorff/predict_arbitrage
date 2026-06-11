"""The IngestionService: drives adapters on a refresh loop.

One ``asyncio`` task runs per enabled adapter. Each task repeatedly fetches
markets, stamps a retrieval timestamp, writes to the ``MarketStore``, and
recomputes staleness, then sleeps for ``refresh_interval`` seconds.

Failure policy is centralized here (Req 1.5): every adapter call is wrapped in
``asyncio.wait_for(timeout=fetch_timeout)`` and any timeout, ``AdapterError``,
or unexpected exception is logged with the adapter name and swallowed so the
other adapters' loops keep running. The store retains the last good data for a
failing adapter.

Behavior mapped to requirements:

- Retrieve from each configured adapter and normalize (Req 1.1, 1.2).
- Loop every ``refresh_interval`` seconds, constrained to <= 60s (Req 1.3).
- Stamp ``retrieved_at`` on every ingested record (Req 1.4).
- On timeout/error, log with the adapter name and continue other adapters,
  retaining last good data (Req 1.5).
- After every cycle, recompute ``is_stale = age_seconds > staleness_threshold``
  for all stored records (Req 1.6, 8.2). The default threshold is 60s (Req 8.3).

A ``clock`` is injectable so staleness and timestamps are deterministic in
tests. It must return timezone-aware UTC ``datetime`` values.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from scanner.adapters.base import AdapterError, PlatformAdapter
from scanner.models import CanonicalMarket
from scanner.store import MarketStore

logger = logging.getLogger(__name__)

# Req 1.3: the refresh interval must not exceed 60 seconds.
MAX_REFRESH_INTERVAL_SECONDS = 60.0

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(market: CanonicalMarket, now: datetime) -> float:
    """Age of ``market``'s data in seconds relative to ``now``.

    ``retrieved_at`` may be naive (assumed UTC) or timezone-aware. Computed
    against the injected ``now`` rather than ``CanonicalMarket.age_seconds`` so
    staleness is deterministic in tests.
    """
    retrieved = market.retrieved_at
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=timezone.utc)
    return (now - retrieved).total_seconds()


class IngestionService:
    """Drives platform adapters on independent refresh loops (Req 1.1-1.6)."""

    def __init__(
        self,
        adapters: Sequence[PlatformAdapter],
        store: MarketStore,
        *,
        refresh_interval: float = 30,
        fetch_timeout: float = 30,
        staleness_threshold: float = 60,
        clock: Optional[Clock] = None,
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive")
        if refresh_interval > MAX_REFRESH_INTERVAL_SECONDS:
            # Req 1.3: prices are refreshed at an interval of 60 seconds or less.
            raise ValueError(
                "refresh_interval must be <= %s seconds (Req 1.3)"
                % MAX_REFRESH_INTERVAL_SECONDS
            )
        if fetch_timeout <= 0:
            raise ValueError("fetch_timeout must be positive")
        if staleness_threshold < 0:
            raise ValueError("staleness_threshold must be >= 0")

        self.adapters: List[PlatformAdapter] = list(adapters)
        self.store = store
        self.refresh_interval = refresh_interval
        self.fetch_timeout = fetch_timeout
        self.staleness_threshold = staleness_threshold
        self._clock: Clock = clock or _default_clock

    async def run(self) -> None:
        """Start one refresh loop per adapter and run until cancelled.

        Each adapter loop is independent: a failure in one adapter's loop does
        not stop the others (Req 1.5). Returns immediately if no adapters are
        configured.
        """
        if not self.adapters:
            return
        tasks = [
            asyncio.ensure_future(self._run_adapter_loop(adapter))
            for adapter in self.adapters
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _run_adapter_loop(self, adapter: PlatformAdapter) -> None:
        """Repeatedly run a cycle for ``adapter``, sleeping between cycles."""
        while True:
            await self._cycle(adapter)
            await asyncio.sleep(self.refresh_interval)

    async def _cycle(self, adapter: PlatformAdapter) -> None:
        """Run one ingestion cycle for a single adapter.

        fetch -> normalize -> timestamp -> store, then recompute staleness for
        all records. On timeout/error the failure is logged and the last good
        data is retained (Req 1.5); staleness is still recomputed so aging
        retained data is flagged.
        """
        try:
            markets = await asyncio.wait_for(
                adapter.fetch_markets(), timeout=self.fetch_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Adapter %r timed out after %ss; retaining last good data.",
                adapter.name,
                self.fetch_timeout,
            )
        except AdapterError as exc:
            logger.warning(
                "Adapter %r failed: %s; retaining last good data.",
                adapter.name,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - isolate any adapter fault
            logger.warning(
                "Adapter %r raised an unexpected error: %s; retaining last good data.",
                adapter.name,
                exc,
            )
        else:
            now = self._clock()
            # Req 1.4: stamp the retrieval timestamp on every ingested record.
            stamped = [
                market.model_copy(update={"retrieved_at": now}) for market in markets
            ]
            # 用 replace_platform 而非 upsert：原子替换该平台的全部市场，删除本轮
            # 不再返回的旧市场，避免消失的市场作为陈旧幽灵无限累积、污染匹配。
            self.store.replace_platform(adapter.name, stamped)
        finally:
            # Req 1.6 / 8.2: recompute staleness for every record each cycle,
            # including for retained data that may have aged past the threshold.
            self._recompute_staleness()

    def _recompute_staleness(self) -> None:
        """Recompute ``is_stale`` for all stored markets (Req 1.6, 8.2)."""
        now = self._clock()
        updated: List[CanonicalMarket] = []
        for market in self.store.list_all():
            is_stale = _age_seconds(market, now) > self.staleness_threshold
            if is_stale != market.is_stale:
                updated.append(market.model_copy(update={"is_stale": is_stale}))
        if updated:
            self.store.upsert(updated)


__all__ = ["IngestionService", "MAX_REFRESH_INTERVAL_SECONDS"]
