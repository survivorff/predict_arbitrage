"""Alert criteria matching and delivery with retry and dedupe (Req 6.2, 6.3, 6.5).

The :class:`AlertService` watches the current set of arbitrage opportunities and
delivers an :class:`Alert` through one or more :class:`AlertChannel`
implementations when a *newly detected* opportunity meets the user's
:class:`AlertCriteria` (Req 6.2). Each alert payload carries the matched event,
the participating platforms, the net profit margin, and the detection timestamp
(Req 6.3).

Delivery is resilient and bounded, upholding Property 11:

- A failing channel is retried with backoff, attempted at most ``max_attempts``
  (default 3) times per opportunity -- "retried at most 3 times" (Req 6.5).
- A given opportunity is alerted at most once until it *clears* (disappears from
  the reported opportunities) and later *reappears*. The service tracks which
  groups are currently alerted and clears that state when a group is no longer
  present, so the same opportunity is not re-alerted every cycle.

Backoff is injectable (the ``sleep`` callable and ``backoff_base``) so tests can
run retry logic deterministically and without real delays.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Iterable, List, Optional, Set

import httpx
from pydantic import BaseModel, Field

try:  # pragma: no cover - typing convenience across Python versions
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from scanner.models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


class AlertCriteria(BaseModel):
    """User-configured conditions an opportunity must meet to trigger an alert.

    - ``min_net_profit_margin``: the opportunity's ``net_profit_margin`` must be
      greater than or equal to this value (Req 6.2).
    - ``min_match_confidence``: when set, the opportunity's match confidence (if
      available on the opportunity) must be greater than or equal to this value.
      The Phase-One :class:`~scanner.models.ArbitrageOpportunity` does not carry
      a confidence value, so this check is skipped when no confidence is exposed.
    - ``platforms``: when set, acts as an allowlist -- every platform the
      opportunity trades on must appear in this list. This keeps alerts
      actionable: an opportunity is only surfaced when *all* of its legs are on
      platforms the user is willing to trade.
    """

    min_net_profit_margin: float = 0.0
    min_match_confidence: Optional[float] = None
    platforms: Optional[List[str]] = None


class Alert(BaseModel):
    """The payload delivered to channels for a qualifying opportunity (Req 6.3)."""

    group_id: str
    event_title: str
    platforms: List[str] = Field(default_factory=list)
    net_profit_margin: float
    detected_at: datetime

    @classmethod
    def from_opportunity(cls, opp: ArbitrageOpportunity) -> "Alert":
        """Build an alert payload from an arbitrage opportunity (Req 6.3).

        The participating platforms are the distinct platforms across the
        opportunity's legs, sorted for deterministic output.
        """
        platforms = sorted({leg.platform for leg in opp.legs})
        return cls(
            group_id=opp.group_id,
            event_title=opp.event_title,
            platforms=platforms,
            net_profit_margin=opp.net_profit_margin,
            detected_at=opp.detected_at,
        )


@runtime_checkable
class AlertChannel(Protocol):
    """A delivery channel for alerts (log / webhook / console)."""

    async def send(self, alert: Alert) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class LogChannel:
    """An :class:`AlertChannel` that records the alert to the logger."""

    level: int = logging.INFO

    async def send(self, alert: Alert) -> None:
        logger.log(
            self.level,
            "ARB ALERT %s | %s | platforms=%s | net_margin=%.4f | detected_at=%s",
            alert.group_id,
            alert.event_title,
            ",".join(alert.platforms),
            alert.net_profit_margin,
            alert.detected_at.isoformat(),
        )


@dataclass
class WebhookChannel:
    """An :class:`AlertChannel` that POSTs the alert as JSON to a webhook URL.

    The :class:`httpx.AsyncClient` is injectable so tests can drive it with
    ``respx`` and production can share a pooled client. When no client is
    provided, one is created per send.
    """

    url: str
    client: Optional[httpx.AsyncClient] = None
    timeout: float = 10.0

    async def send(self, alert: Alert) -> None:
        payload = alert.model_dump(mode="json")
        if self.client is not None:
            response = await self.client.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return
        async with httpx.AsyncClient() as client:
            response = await client.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()


# Default async sleep used for backoff; injectable for deterministic tests.
async def _default_sleep(seconds: float) -> None:  # pragma: no cover - thin wrapper
    await asyncio.sleep(seconds)


def format_alert_text(alert: "Alert") -> str:
    """把告警格式化为人类可读的纯文本（Telegram/Lark 共用）。

    机会几分钟即逝，告警须一眼可判：事件、平台、净利润率、时间。刻意精简、可直接转发。
    """
    return (
        "🔔 跨平台套利信号\n"
        f"事件：{alert.event_title}\n"
        f"平台：{' × '.join(alert.platforms)}\n"
        f"净利润率：{alert.net_profit_margin * 100:.2f}%\n"
        f"检测时间：{alert.detected_at.isoformat()}\n"
        f"组：{alert.group_id}\n"
        "⚠️ 请在下单前核对两市场是否为同一事件（结算口径/日期/极性）。"
    )


@dataclass
class TelegramChannel:
    """把告警推送到 Telegram 的 :class:`AlertChannel`（P0 实时告警）。

    通过 Bot API ``sendMessage`` 发送。``token``（@BotFather 获取）与 ``chat_id`` 只经
    环境变量注入、绝不入库/入日志。``client`` 可注入以便测试（respx）与连接池复用。
    """

    token: str
    chat_id: str
    client: Optional[httpx.AsyncClient] = None
    timeout: float = 10.0

    async def send(self, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": format_alert_text(alert)}
        if self.client is not None:
            resp = await self.client.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()


@dataclass
class LarkChannel:
    """把告警推送到飞书/Lark 群机器人的 :class:`AlertChannel`（P0 实时告警）。

    POST 到群自定义机器人的 ``webhook`` URL（只经环境变量注入）。发送纯文本消息。
    """

    webhook_url: str
    client: Optional[httpx.AsyncClient] = None
    timeout: float = 10.0

    async def send(self, alert: Alert) -> None:
        payload = {"msg_type": "text", "content": {"text": format_alert_text(alert)}}
        if self.client is not None:
            resp = await self.client.post(self.webhook_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.webhook_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()


@dataclass
class AlertService:
    """Matches new opportunities to criteria and delivers alerts with retry.

    Args:
        channels: The alert channels to deliver through.
        criteria: The user's alert criteria (Req 6.2).
        max_attempts: Maximum number of delivery attempts per channel for a
            single opportunity. A failing channel is attempted at most this many
            times -- "retried at most 3 times" (Req 6.5, Property 11).
        backoff_base: Base seconds for exponential backoff between attempts.
        sleep: Async sleep callable used for backoff; injectable for tests.
    """

    channels: List[AlertChannel]
    criteria: AlertCriteria = field(default_factory=AlertCriteria)
    max_attempts: int = 3
    backoff_base: float = 0.5
    sleep: Callable[[float], Awaitable[None]] = _default_sleep
    _alerted: Set[str] = field(default_factory=set, init=False)

    async def on_new_opportunities(
        self, opps: Iterable[ArbitrageOpportunity]
    ) -> List[Alert]:
        """Process the current opportunities, delivering alerts for new ones.

        Dedupe semantics (Property 11): an opportunity for a given group is
        alerted at most once until it clears and reappears. Groups that are no
        longer present in ``opps`` have their alerted state cleared, so they can
        alert again if they later reappear. Returns the alerts that fired this
        call.
        """
        opps = list(opps)
        present_group_ids = {opp.group_id for opp in opps}

        # Clear alerted state for groups that have disappeared so they can
        # re-alert when they reappear.
        self._alerted &= present_group_ids

        fired: List[Alert] = []
        for opp in opps:
            if opp.group_id in self._alerted:
                continue
            if not self._matches(opp):
                continue
            alert = Alert.from_opportunity(opp)
            delivered = await self._deliver(alert)
            # 仅在至少一个通道投递成功后才标记已告警；全部失败则保留未告警状态，
            # 下个周期会重试（持续存在的真实机会不应因一次投递失败而被永久丢弃）。
            if delivered:
                self._alerted.add(opp.group_id)
                fired.append(alert)
        return fired

    def _matches(self, opp: ArbitrageOpportunity) -> bool:
        """True when an opportunity satisfies the configured criteria (Req 6.2)."""
        if opp.net_profit_margin < self.criteria.min_net_profit_margin:
            return False

        if self.criteria.min_match_confidence is not None:
            confidence = getattr(opp, "match_confidence", None)
            if confidence is not None and confidence < self.criteria.min_match_confidence:
                return False

        if self.criteria.platforms is not None:
            allowed = set(self.criteria.platforms)
            opp_platforms = {leg.platform for leg in opp.legs}
            if not opp_platforms <= allowed:
                return False

        return True

    async def _deliver(self, alert: Alert) -> bool:
        """Deliver an alert to every channel, retrying failures with backoff.

        Returns True if at least one channel delivered successfully. When every
        channel fails, returns False so the caller can leave the opportunity
        un-alerted and retry on a later cycle (a persistent opportunity should
        not be silently dropped just because delivery failed once).
        """
        any_ok = False
        for channel in self.channels:
            if await self._deliver_to_channel(channel, alert):
                any_ok = True
        return any_ok

    async def _deliver_to_channel(self, channel: AlertChannel, alert: Alert) -> bool:
        """Attempt delivery to one channel up to ``max_attempts`` times (Req 6.5).

        Logs each failure and backs off between attempts. Returns True on
        success, False if every attempt fails (the failure is logged and
        swallowed so one bad channel cannot block the others or the pipeline;
        Property 11 bounds the attempts).
        """
        attempts = max(1, self.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                await channel.send(alert)
                return True
            except Exception as exc:  # noqa: BLE001 - channels may raise anything
                logger.warning(
                    "Alert delivery failed for group %s on %s (attempt %d/%d): %s",
                    alert.group_id,
                    type(channel).__name__,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    await self.sleep(self.backoff_base * (2 ** (attempt - 1)))
        logger.error(
            "Alert delivery permanently failed for group %s on %s after %d attempts",
            alert.group_id,
            type(channel).__name__,
            attempts,
        )
        return False


__all__ = [
    "AlertCriteria",
    "Alert",
    "AlertChannel",
    "LogChannel",
    "WebhookChannel",
    "TelegramChannel",
    "LarkChannel",
    "format_alert_text",
    "AlertService",
]
