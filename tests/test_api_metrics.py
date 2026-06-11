"""/metrics 端点测试（Phase Two · 切片 D）。

用 FastAPI ``TestClient`` 驱动 ``create_app``：

- 未配置 metrics（不传 ``metrics``）时 GET /metrics 返回 ``{}`` 且状态码 200。
- 配置了已 record_cycle 过的 :class:`PipelineMetrics` 时，GET /metrics 返回其
  ``snapshot()``，字段齐全（cycles_total、active_signals_count 等）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from scanner.api import create_app
from scanner.observability import PipelineMetrics
from scanner.store import InMemoryMarketStore

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定性、可前进的 UTC 时钟（供注入）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def test_metrics_endpoint_returns_empty_when_not_configured():
    # 未配置 metrics 时返回空对象且状态码 200。
    client = TestClient(create_app(market_store=InMemoryMarketStore()))

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.json() == {}


def test_metrics_endpoint_returns_snapshot_when_configured():
    # 配置了已 record_cycle 的 metrics 时返回其快照，字段齐全。
    clock = FakeClock(BASE_TIME)
    metrics = PipelineMetrics(clock=clock)
    metrics.record_cycle(
        duration_seconds=1.5,
        markets_count=7,
        groups_count=3,
        opportunities_count=2,
        active_signals_count=4,
        signals_opened=2,
        signals_closed=1,
        alerts_fired=2,
    )

    client = TestClient(
        create_app(market_store=InMemoryMarketStore(), metrics=metrics)
    )

    resp = client.get("/metrics")

    assert resp.status_code == 200
    body = resp.json()
    # 与直接 snapshot() 一致。
    assert body == metrics.snapshot()
    # 字段齐全。
    assert body["cycles_total"] == 1
    assert body["signals_opened_total"] == 2
    assert body["signals_closed_total"] == 1
    assert body["alerts_fired_total"] == 2
    assert body["last_cycle_duration_seconds"] == 1.5
    assert body["last_markets_count"] == 7
    assert body["last_groups_count"] == 3
    assert body["last_opportunities_count"] == 2
    assert body["active_signals_count"] == 4
    assert body["last_cycle_at"] == BASE_TIME.isoformat()
    assert "clock" not in body
