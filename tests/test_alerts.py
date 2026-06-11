"""Unit tests for the AlertService, channels, retry, and dedupe (Req 6.2, 6.3, 6.5).

Covers:
- Criteria matching: margin threshold and platform allowlist (Req 6.2).
- Alert payload contents: event, platforms, net margin, timestamp (Req 6.3).
- Retry up to 3 attempts on a failing channel (Req 6.5).
- Dedupe: an opportunity is alerted at most once until it clears and reappears.
- WebhookChannel POSTs JSON via an injected httpx client (mocked with respx).

Validates: Property 11 (alert delivery bound + dedupe).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import httpx
import pytest
import respx

from scanner.alerts import (
    Alert,
    AlertCriteria,
    AlertService,
    LogChannel,
    WebhookChannel,
)
from scanner.models import ArbLeg, ArbitrageOpportunity

NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _opp(
    group_id: str,
    margin: float,
    platforms: List[str] = ("polymarket", "kalshi"),
    title: str = None,
) -> ArbitrageOpportunity:
    legs = [
        ArbLeg(platform=p, market_id=f"m-{p}", outcome="YES", price=0.4)
        for p in platforms
    ]
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=title if title is not None else f"event {group_id}",
        legs=legs,
        net_profit_margin=margin,
        recommended_size_usd=100.0,
        detected_at=NOW,
        data_age_seconds=1.0,
    )


# ---------------------------------------------------------------------------
# Test channels
# ---------------------------------------------------------------------------

class RecordingChannel:
    """Channel that records every alert it receives."""

    def __init__(self) -> None:
        self.received: List[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.received.append(alert)


class FailingChannel:
    """Channel that fails a fixed number of times before optionally succeeding."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    async def send(self, alert: Alert) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"boom #{self.attempts}")


async def _no_sleep(_seconds: float) -> None:
    return None


def _service(channels, **kwargs) -> AlertService:
    kwargs.setdefault("sleep", _no_sleep)
    kwargs.setdefault("backoff_base", 0.0)
    return AlertService(channels=channels, **kwargs)


# ---------------------------------------------------------------------------
# Criteria matching (Req 6.2)
# ---------------------------------------------------------------------------

async def test_fires_when_margin_meets_threshold():
    channel = RecordingChannel()
    service = _service([channel], criteria=AlertCriteria(min_net_profit_margin=0.05))
    fired = await service.on_new_opportunities([_opp("g1", 0.10)])
    assert [a.group_id for a in fired] == ["g1"]
    assert len(channel.received) == 1


async def test_does_not_fire_below_margin_threshold():
    channel = RecordingChannel()
    service = _service([channel], criteria=AlertCriteria(min_net_profit_margin=0.05))
    fired = await service.on_new_opportunities([_opp("g1", 0.02)])
    assert fired == []
    assert channel.received == []


async def test_margin_exactly_at_threshold_fires():
    channel = RecordingChannel()
    service = _service([channel], criteria=AlertCriteria(min_net_profit_margin=0.05))
    fired = await service.on_new_opportunities([_opp("g1", 0.05)])
    assert [a.group_id for a in fired] == ["g1"]


async def test_platform_allowlist_excludes_unlisted_platform():
    channel = RecordingChannel()
    service = _service(
        [channel],
        criteria=AlertCriteria(min_net_profit_margin=0.0, platforms=["polymarket"]),
    )
    # Opportunity trades on kalshi too -> not all legs allowed -> excluded.
    fired = await service.on_new_opportunities(
        [_opp("g1", 0.1, platforms=["polymarket", "kalshi"])]
    )
    assert fired == []


async def test_platform_allowlist_includes_when_all_legs_allowed():
    channel = RecordingChannel()
    service = _service(
        [channel],
        criteria=AlertCriteria(
            min_net_profit_margin=0.0, platforms=["polymarket", "kalshi"]
        ),
    )
    fired = await service.on_new_opportunities(
        [_opp("g1", 0.1, platforms=["polymarket", "kalshi"])]
    )
    assert [a.group_id for a in fired] == ["g1"]


async def test_only_qualifying_opportunities_fire_in_mixed_batch():
    channel = RecordingChannel()
    service = _service([channel], criteria=AlertCriteria(min_net_profit_margin=0.05))
    fired = await service.on_new_opportunities(
        [_opp("low", 0.01), _opp("ok", 0.2), _opp("also", 0.5)]
    )
    assert {a.group_id for a in fired} == {"ok", "also"}


# ---------------------------------------------------------------------------
# Payload contents (Req 6.3)
# ---------------------------------------------------------------------------

async def test_alert_payload_contents():
    channel = RecordingChannel()
    service = _service([channel])
    await service.on_new_opportunities(
        [_opp("g1", 0.123, platforms=["kalshi", "polymarket"], title="US Election")]
    )
    alert = channel.received[0]
    assert alert.event_title == "US Election"
    assert alert.platforms == ["kalshi", "polymarket"]  # sorted, deduped
    assert alert.net_profit_margin == pytest.approx(0.123)
    assert alert.detected_at == NOW
    assert alert.group_id == "g1"


async def test_alert_platforms_deduped_and_sorted():
    opp = _opp("g1", 0.1, platforms=["polymarket", "kalshi", "polymarket"])
    alert = Alert.from_opportunity(opp)
    assert alert.platforms == ["kalshi", "polymarket"]


# ---------------------------------------------------------------------------
# Retry up to 3 (Req 6.5, Property 11)
# ---------------------------------------------------------------------------

async def test_retries_then_succeeds():
    # Fails twice then succeeds on the third attempt.
    channel = FailingChannel(fail_times=2)
    service = _service([channel], max_attempts=3)
    await service.on_new_opportunities([_opp("g1", 0.1)])
    assert channel.attempts == 3


async def test_retries_at_most_max_attempts_on_persistent_failure():
    # Property 11: a failing channel is attempted at most 3 times per opportunity.
    channel = FailingChannel(fail_times=99)
    service = _service([channel], max_attempts=3)
    # Should not raise even though the channel always fails.
    fired = await service.on_new_opportunities([_opp("g1", 0.1)])
    assert channel.attempts == 3
    # 投递全部失败 → 不计入 fired（也不标记已告警，下个周期会重试）。
    assert fired == []


async def test_backoff_sleep_called_between_attempts():
    channel = FailingChannel(fail_times=3)
    sleeps: List[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = AlertService(
        channels=[channel], max_attempts=3, backoff_base=0.5, sleep=record_sleep
    )
    await service.on_new_opportunities([_opp("g1", 0.1)])
    # 3 attempts -> 2 backoff sleeps between them.
    assert len(sleeps) == 2
    assert sleeps == [0.5, 1.0]


async def test_failing_channel_does_not_block_other_channels():
    failing = FailingChannel(fail_times=99)
    recording = RecordingChannel()
    service = _service([failing, recording], max_attempts=3)
    await service.on_new_opportunities([_opp("g1", 0.1)])
    assert failing.attempts == 3
    assert len(recording.received) == 1


# ---------------------------------------------------------------------------
# Dedupe (Property 11)
# ---------------------------------------------------------------------------

async def test_same_opportunity_alerted_once_across_cycles():
    channel = RecordingChannel()
    service = _service([channel])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    assert len(channel.received) == 1


async def test_opportunity_realerts_after_clearing_and_reappearing():
    channel = RecordingChannel()
    service = _service([channel])
    await service.on_new_opportunities([_opp("g1", 0.1)])  # alert
    await service.on_new_opportunities([])  # clears
    await service.on_new_opportunities([_opp("g1", 0.1)])  # reappears -> alert
    assert len(channel.received) == 2


async def test_dedupe_is_per_group():
    channel = RecordingChannel()
    service = _service([channel])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    fired = await service.on_new_opportunities([_opp("g1", 0.1), _opp("g2", 0.2)])
    assert [a.group_id for a in fired] == ["g2"]
    assert {a.group_id for a in channel.received} == {"g1", "g2"}


async def test_failed_delivery_is_retried_next_cycle():
    # 修复：投递全部失败的机会不应被永久去重——它在持续存在时下个周期会再次尝试，
    # 直到投递成功才标记已告警（持续存在的真实机会不应因一次投递失败而被丢弃）。
    channel = FailingChannel(fail_times=3)  # 第 1 周期 3 次全失败，之后成功
    service = _service([channel], max_attempts=3)
    fired1 = await service.on_new_opportunities([_opp("g1", 0.1)])
    assert fired1 == []  # 第 1 周期投递失败
    assert channel.attempts == 3
    # 第 2 周期：机会仍在 → 重试 → 这次成功 → 标记并 fired。
    fired2 = await service.on_new_opportunities([_opp("g1", 0.1)])
    assert [a.group_id for a in fired2] == ["g1"]
    assert channel.attempts == 4  # 第 2 周期第一次尝试即成功


async def test_successful_alert_is_deduped_until_cleared():
    # 投递成功后正常去重：持续存在不重复告警，直到清除并重现。
    channel = RecordingChannel()
    service = _service([channel])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    await service.on_new_opportunities([_opp("g1", 0.1)])
    assert len(channel.received) == 1  # 成功投递后不再重复


# ---------------------------------------------------------------------------
# WebhookChannel (httpx POST, mocked with respx)
# ---------------------------------------------------------------------------

@respx.mock
async def test_webhook_channel_posts_json():
    route = respx.post("https://hooks.example.com/arb").mock(
        return_value=httpx.Response(200)
    )
    alert = Alert.from_opportunity(_opp("g1", 0.1, title="Event X"))
    async with httpx.AsyncClient() as client:
        channel = WebhookChannel(url="https://hooks.example.com/arb", client=client)
        await channel.send(alert)
    assert route.called
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["group_id"] == "g1"
    assert body["event_title"] == "Event X"
    assert body["net_profit_margin"] == pytest.approx(0.1)


@respx.mock
async def test_webhook_channel_raises_on_http_error_and_is_retried():
    route = respx.post("https://hooks.example.com/arb").mock(
        return_value=httpx.Response(500)
    )
    async with httpx.AsyncClient() as client:
        channel = WebhookChannel(url="https://hooks.example.com/arb", client=client)
        service = _service([channel], max_attempts=3)
        await service.on_new_opportunities([_opp("g1", 0.1)])
    # 3 attempts against the failing webhook (Req 6.5).
    assert route.call_count == 3


@respx.mock
async def test_webhook_channel_succeeds_via_service():
    route = respx.post("https://hooks.example.com/arb").mock(
        return_value=httpx.Response(200)
    )
    async with httpx.AsyncClient() as client:
        channel = WebhookChannel(url="https://hooks.example.com/arb", client=client)
        service = _service([channel])
        fired = await service.on_new_opportunities([_opp("g1", 0.1)])
    assert route.call_count == 1
    assert [a.group_id for a in fired] == ["g1"]


# ---------------------------------------------------------------------------
# LogChannel
# ---------------------------------------------------------------------------

async def test_log_channel_emits_record(caplog):
    import logging as _logging

    channel = LogChannel()
    alert = Alert.from_opportunity(_opp("g1", 0.1, title="Logged Event"))
    with caplog.at_level(_logging.INFO, logger="scanner.alerts"):
        await channel.send(alert)
    assert any("Logged Event" in rec.message or "Logged Event" in rec.getMessage()
               for rec in caplog.records)
