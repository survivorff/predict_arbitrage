"""End-to-end pipeline integration tests (Task 15).

Drives the fully wired :class:`~scanner.app.ScannerApplication` with two scripted
``FakeAdapter`` instances (one standing in for Polymarket, one for Kalshi) whose
binary YES/NO markets describe the *same* event with matching titles, so the
:class:`~scanner.matching.MatchingEngine` groups them. A third adapter always
fails to prove failure isolation.

The scripted price timeline:

- **Cycle 1** — ``ask_yes(poly) + ask_no(kalshi) < 1``: a profitable arbitrage
  exists, an opportunity is listed, and an alert fires (with retry on a failing
  channel).
- **Cycle 1 (repeat)** — same prices: dedupe means the opportunity is *not*
  re-alerted.
- **Cycle 2** — prices converge so the assembled cost ``>= 1``: the net margin
  drops to ``<= 0`` and the opportunity disappears from the listing.

Everything is driven deterministically: a fixed clock is injected so data is
never stale, and alert backoff sleeps are stubbed to a no-op so retry logic runs
without real delays.

Covers:
- Property 4 (failure isolation): a failing adapter does not block the others.
- Property 10 (opportunity ordering / no non-positive margins).
- Property 11 (alert retry bound and dedupe-until-cleared).
- Req 1.1, 6.2, 6.4, 6.5, 7.1, 7.4.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import pytest

from scanner.adapters.base import AdapterError
from scanner.app import build_application
from scanner.config import load_config_from_dict
from scanner.models import CanonicalMarket, Outcome
from tests.adapter_contract import FakeAdapter

FIXED_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

EVENT_TITLE = "Will Bitcoin close above 100000 in 2025?"


def _fixed_clock() -> datetime:
    """A constant clock so ingested data is always fresh (age == 0)."""
    return FIXED_NOW


def _market(
    platform: str,
    market_id: str,
    *,
    yes_ask: float,
    no_ask: float,
    liquidity: float = 1000.0,
) -> CanonicalMarket:
    """A binary YES/NO market for the shared event with explicit ask prices."""
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=EVENT_TITLE,
        outcomes=[
            Outcome(
                name="YES",
                price=yes_ask,
                bid=max(0.0, yes_ask - 0.01),
                ask=yes_ask,
                available_liquidity_usd=liquidity,
            ),
            Outcome(
                name="NO",
                price=no_ask,
                bid=max(0.0, no_ask - 0.01),
                ask=no_ask,
                available_liquidity_usd=liquidity,
            ),
        ],
        volume_usd=50000.0,
        liquidity_usd=liquidity,
        fee_rate=0.0,
        retrieved_at=FIXED_NOW,
    )


# --------------------------------------------------------------------------- #
# Test alert channels
# --------------------------------------------------------------------------- #


class RecordingChannel:
    """An alert channel that records every alert it successfully receives."""

    def __init__(self) -> None:
        self.alerts: List = []

    async def send(self, alert) -> None:
        self.alerts.append(alert)


class CountingFailChannel:
    """An alert channel that always fails, counting every delivery attempt."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, alert) -> None:
        self.attempts += 1
        raise RuntimeError("channel down")


async def _no_sleep(_seconds: float) -> None:
    """Stubbed backoff so retry logic runs with no real delay."""
    return None


def _build_app(recording: RecordingChannel, failing: CountingFailChannel):
    """Wire the application with scripted adapters and the test channels."""
    config = load_config_from_dict(
        {
            "scanner": {
                "refresh_interval_seconds": 30,
                "fetch_timeout_seconds": 30,
                "staleness_threshold_seconds": 60,
                "match_confidence_min": 0.6,
            },
            "platforms": [
                {"name": "polymarket", "enabled": True, "fee_model": {"type": "flat", "rate": 0.0}},
                {"name": "kalshi", "enabled": True, "fee_model": {"type": "kalshi"}},
            ],
            "alerts": {
                "channels": ["log"],
                "criteria": {"min_net_profit_margin": 0.02},
            },
        }
    )

    # Polymarket: cheap YES. Kalshi: cheap NO. Cycle 1 (and its repeat) is
    # profitable; cycle 2 converges so no arbitrage remains.
    poly = FakeAdapter(
        name="polymarket",
        responses=[
            [_market("polymarket", "btc-100k", yes_ask=0.40, no_ask=0.62)],  # cycle 1
            [_market("polymarket", "btc-100k", yes_ask=0.40, no_ask=0.62)],  # cycle 1 repeat
            [_market("polymarket", "btc-100k", yes_ask=0.55, no_ask=0.50)],  # cycle 2 converged
        ],
    )
    kalshi = FakeAdapter(
        name="kalshi",
        responses=[
            [_market("kalshi", "BTC-100K", yes_ask=0.55, no_ask=0.45)],  # cycle 1
            [_market("kalshi", "BTC-100K", yes_ask=0.55, no_ask=0.45)],  # cycle 1 repeat
            [_market("kalshi", "BTC-100K", yes_ask=0.56, no_ask=0.52)],  # cycle 2 converged
        ],
    )
    # A third adapter that always fails: proves failure isolation (Property 4).
    broken = FakeAdapter(name="broken", fetch_error=AdapterError("boom", adapter="broken"))

    application = build_application(
        config,
        adapters=[poly, kalshi, broken],
        alert_channels=[recording, failing],
        clock=_fixed_clock,
        alert_sleep=_no_sleep,
        alert_backoff_base=0.0,
    )
    return application


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_opportunity_appears_and_alert_fires_with_retry():
    """Cycle 1: a profitable opportunity is listed and an alert fires (Req 6.2)."""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    fired = await app.ingest_once_and_run_pipeline()

    # Req 1.1 / Property 4: both healthy adapters ingested despite the broken one.
    platforms = {m.platform for m in app.market_store.list_all()}
    assert "polymarket" in platforms
    assert "kalshi" in platforms
    assert "broken" not in platforms

    # An opportunity is listed and an alert fired (Req 6.2).
    listed = app.opportunity_service.list()
    assert len(listed) == 1
    assert listed[0].net_profit_margin > 0
    assert len(fired) == 1

    # The alert carries both participating platforms (Req 6.3).
    assert set(fired[0].platforms) == {"polymarket", "kalshi"}

    # The succeeding channel received the alert.
    assert len(recording.alerts) == 1
    # Req 6.5 / Property 11: the failing channel was retried at most 3 times.
    assert failing.attempts == 3


async def test_alert_is_deduped_until_opportunity_clears():
    """A still-present opportunity is not re-alerted on the next cycle (Property 11)."""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    first = await app.ingest_once_and_run_pipeline()  # cycle 1: alert fires
    assert len(first) == 1
    assert failing.attempts == 3

    second = await app.ingest_once_and_run_pipeline()  # cycle 1 repeat: dedupe
    assert second == []
    # No additional deliveries to either channel.
    assert len(recording.alerts) == 1
    assert failing.attempts == 3


async def test_opportunity_disappears_when_prices_converge():
    """Cycle 2: margin drops to <= 0 so the opportunity is removed (Req 6.4)."""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    await app.ingest_once_and_run_pipeline()  # cycle 1: opportunity present
    assert len(app.opportunity_service.list()) == 1

    await app.ingest_once_and_run_pipeline()  # cycle 1 repeat: still present
    assert len(app.opportunity_service.list()) == 1

    await app.ingest_once_and_run_pipeline()  # cycle 2: prices converge

    # Req 6.4 / Property 10: no non-positive-margin opportunity remains listed.
    listed = app.opportunity_service.list()
    assert listed == []
    assert app.opportunity_store.get("polymarket:btc-100k|kalshi:BTC-100K") is None


async def test_opportunity_listing_is_sorted_and_positive(monkeypatch):
    """Property 10: the listing is sorted desc and contains no margin <= 0."""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    await app.ingest_once_and_run_pipeline()

    listed = app.opportunity_service.list()
    margins = [o.net_profit_margin for o in listed]
    assert all(m > 0 for m in margins)
    assert margins == sorted(margins, reverse=True)


async def test_recommended_size_bounded_by_liquidity():
    """Recommended size never exceeds the thinnest leg's liquidity (Property 9)."""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    await app.ingest_once_and_run_pipeline()

    listed = app.opportunity_service.list()
    assert len(listed) == 1
    # Both legs carry 1000 USD of depth in cycle 1.
    assert listed[0].recommended_size_usd <= 1000.0


# --------------------------------------------------------------------------- #
# 信号生命周期端到端（Phase Two · 切片 A）
# --------------------------------------------------------------------------- #


async def test_signal_opens_then_closes_across_pipeline_cycles():
    """端到端：套利出现时开启信号并产出 OPENED 事件；价差收敛后信号被 CLOSED。"""
    from scanner.models import SignalEventType, SignalStatus

    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    # 周期 1：存在可列出的套利机会 → 应开启一个信号。
    await app.ingest_once_and_run_pipeline()

    active = app.signal_service.store.list_active()
    assert len(active) == 1
    signal = active[0]
    # 首周期为 OPEN（同一 group 此前未见过）。
    assert signal.status is SignalStatus.OPEN
    assert signal.is_active is True

    events = app.signal_service.store.list_events()
    assert any(e.event_type is SignalEventType.OPENED for e in events)

    # 周期 1 重复：同一机会仍在 → 信号转 SUSTAINED 并保持活跃。
    await app.ingest_once_and_run_pipeline()
    active = app.signal_service.store.list_active()
    assert len(active) == 1
    assert active[0].status is SignalStatus.SUSTAINED

    # 周期 2：价格收敛，机会消失 → 信号被关闭，活跃集合清空。
    await app.ingest_once_and_run_pipeline()

    assert app.signal_service.store.list_active() == []
    closed_events = [
        e
        for e in app.signal_service.store.list_events()
        if e.event_type is SignalEventType.CLOSED
    ]
    assert len(closed_events) == 1
    assert closed_events[0].status is SignalStatus.CLOSED


# --------------------------------------------------------------------------- #
# 可观测性指标埋点端到端（Phase Two · 切片 D）
# --------------------------------------------------------------------------- #


async def test_pipeline_updates_metrics_cycles_and_signal_opens():
    """跑一轮流水线后 metrics 应反映周期递增与本周期开启的信号。"""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    # 初始无周期。
    assert app.metrics.cycles_total == 0

    await app.ingest_once_and_run_pipeline()

    # 周期数递增；本周期开启了套利信号（signals_opened_total >= 1）。
    assert app.metrics.cycles_total == 1
    assert app.metrics.signals_opened_total >= 1
    # 本周期存在可列出的机会与活跃信号。
    assert app.metrics.last_opportunities_count >= 1
    assert app.metrics.active_signals_count >= 1

    # 再跑一轮（同样的机会仍在）：周期数继续递增，累计开启量不减。
    opened_after_first = app.metrics.signals_opened_total
    await app.ingest_once_and_run_pipeline()
    assert app.metrics.cycles_total == 2
    assert app.metrics.signals_opened_total >= opened_after_first


async def test_pipeline_metrics_records_signal_close():
    """价差收敛后本轮应记录信号关闭（signals_closed_total 递增）。"""
    recording = RecordingChannel()
    failing = CountingFailChannel()
    app = _build_app(recording, failing)

    await app.ingest_once_and_run_pipeline()  # 周期 1：开启
    await app.ingest_once_and_run_pipeline()  # 周期 1 重复：维持
    assert app.metrics.signals_closed_total == 0

    await app.ingest_once_and_run_pipeline()  # 周期 2：收敛 → 关闭

    assert app.metrics.cycles_total == 3
    assert app.metrics.signals_closed_total >= 1
    assert app.metrics.active_signals_count == 0
