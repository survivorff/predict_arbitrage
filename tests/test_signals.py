"""SignalService 单元测试（Phase Two · 切片 A）。

用可前进的 ``FakeClock`` 注入时钟，使时间戳与时长确定可控；用手工构造的
``ArbitrageOpportunity`` 快照逐周期驱动 ``SignalService.reconcile``，验证信号在
``OPEN → SUSTAINED → CLOSED`` 之间的状态转移与事件流产出。

覆盖：
- 新机会开启 OPEN 信号 + OPENED 事件。
- 同一 group 持续 → SUSTAINED + UPDATED 事件，last_seen_at 推进、first 不变、时长增加。
- 峰值净利润率单调不减（Property 14 相关）。
- group 消失 → CLOSED 事件 + 从活跃集合移除、closed_at 被设置。
- CLOSED 后重新出现 → 开启全新 OPEN 信号（Property 13）。
- 事件与状态一致：每周期每个受影响 group 至多 1 个事件，类型与状态对应（Property 14）。
- 多 group 混合：新开/持续/关闭并存，事件集合正确。
- list_events() 累积全部历史事件且按发生顺序。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from scanner.models import (
    ArbLeg,
    ArbitrageOpportunity,
    SignalEventType,
    SignalStatus,
)
from scanner.signals import InMemorySignalStore, SignalService

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟，供注入使用。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _opp(
    group_id: str,
    net_profit_margin: float,
    *,
    event_title: str = "事件",
    recommended_size_usd: float = 100.0,
    data_age_seconds: float = 5.0,
    detected_at: datetime = BASE_TIME,
) -> ArbitrageOpportunity:
    """构造一个机会快照，带一条买腿。"""
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=event_title,
        legs=[
            ArbLeg(platform="polymarket", market_id=f"{group_id}-m", outcome="YES", price=0.4),
        ],
        net_profit_margin=net_profit_margin,
        recommended_size_usd=recommended_size_usd,
        detected_at=detected_at,
        data_age_seconds=data_age_seconds,
    )


def _service() -> SignalService:
    """构造一个带内存存储和 FakeClock 的服务。"""
    return SignalService(store=InMemorySignalStore(), clock=FakeClock())


# --------------------------------------------------------------------------- #
# 新机会 → 开启 OPEN 信号
# --------------------------------------------------------------------------- #

def test_new_opportunity_opens_signal():
    # 新出现的 group 应开启 OPEN 信号并产出唯一一个 OPENED 事件。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    events = service.reconcile([_opp("g1", 0.10)])

    assert len(events) == 1
    event = events[0]
    assert event.event_type is SignalEventType.OPENED
    assert event.status is SignalStatus.OPEN
    assert event.occurred_at == BASE_TIME

    active = service.store.list_active()
    assert len(active) == 1
    signal = active[0]
    assert signal.group_id == "g1"
    assert signal.status is SignalStatus.OPEN
    # 首次/最近检测时间都为当前时钟。
    assert signal.first_detected_at == BASE_TIME
    assert signal.last_seen_at == BASE_TIME
    # 峰值初始化为当前净利润率。
    assert signal.peak_net_profit_margin == 0.10
    assert signal.is_active is True
    assert signal.closed_at is None
    # 首周期时长为 0。
    assert signal.duration_seconds == 0.0


# --------------------------------------------------------------------------- #
# 同一 group 持续 → SUSTAINED
# --------------------------------------------------------------------------- #

def test_sustained_signal_advances_last_seen_and_duration():
    # 第二周期同一 group 仍在 → 转 SUSTAINED，产出 UPDATED 事件。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_opp("g1", 0.10)])  # 周期 1：OPEN
    clock.advance(30)
    events = service.reconcile([_opp("g1", 0.12)])  # 周期 2：SUSTAINED

    assert len(events) == 1
    assert events[0].event_type is SignalEventType.UPDATED
    assert events[0].status is SignalStatus.SUSTAINED

    signal = service.store.get_active("g1")
    assert signal.status is SignalStatus.SUSTAINED
    # first_detected_at 不变，last_seen_at 推进。
    assert signal.first_detected_at == BASE_TIME
    assert signal.last_seen_at == BASE_TIME + timedelta(seconds=30)
    # 净利润率与规模被刷新。
    assert signal.net_profit_margin == 0.12
    # 时长增加。
    assert signal.duration_seconds == 30.0


# --------------------------------------------------------------------------- #
# 峰值净利润率单调不减
# --------------------------------------------------------------------------- #

def test_peak_net_profit_margin_is_monotonic():
    # 净利润率先升后降，峰值应记录最高点而不随当前下降。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_opp("g1", 0.10)])  # 峰值 0.10
    clock.advance(10)
    service.reconcile([_opp("g1", 0.25)])  # 升至 0.25
    clock.advance(10)
    service.reconcile([_opp("g1", 0.08)])  # 降回 0.08

    signal = service.store.get_active("g1")
    assert signal.net_profit_margin == 0.08
    # 峰值保持在历史最高 0.25。
    assert signal.peak_net_profit_margin == 0.25


# --------------------------------------------------------------------------- #
# group 消失 → CLOSED
# --------------------------------------------------------------------------- #

def test_disappearing_group_closes_signal():
    # group 在本周期消失 → 产出 CLOSED 事件，信号从活跃集合移除，closed_at 被设置。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_opp("g1", 0.10)])
    clock.advance(45)
    events = service.reconcile([])  # 机会消失

    assert len(events) == 1
    assert events[0].event_type is SignalEventType.CLOSED
    assert events[0].status is SignalStatus.CLOSED

    # 从活跃集合移除。
    assert service.store.list_active() == []
    assert service.store.get_active("g1") is None

    # CLOSED 信号被追加到事件流并带 closed_at；duration 从首检测到关闭。
    closed_event = events[0]
    assert closed_event.duration_seconds == 45.0


# --------------------------------------------------------------------------- #
# CLOSED 后重新出现 → 全新信号（Property 13）
# --------------------------------------------------------------------------- #

def test_reopened_group_starts_fresh_signal():
    # 同一 group 关闭后重新出现，应开启一个全新 OPEN 信号，而非复活旧信号。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_opp("g1", 0.10)])  # t=0 开启
    clock.advance(20)
    service.reconcile([])  # t=20 关闭
    clock.advance(20)
    events = service.reconcile([_opp("g1", 0.30)])  # t=40 重新出现

    # 重新出现产出 OPENED 事件（不是 UPDATED）。
    assert len(events) == 1
    assert events[0].event_type is SignalEventType.OPENED
    assert events[0].status is SignalStatus.OPEN

    signal = service.store.get_active("g1")
    assert signal.status is SignalStatus.OPEN
    # first_detected_at 为新的时间，而非最初的 BASE_TIME。
    assert signal.first_detected_at == BASE_TIME + timedelta(seconds=40)
    assert signal.last_seen_at == BASE_TIME + timedelta(seconds=40)
    # 峰值从新信号重新开始，不携带旧峰值。
    assert signal.peak_net_profit_margin == 0.30
    assert signal.closed_at is None
    # 活跃集合中该 group 只有一个信号（Property 13）。
    assert len([s for s in service.store.list_active() if s.group_id == "g1"]) == 1


# --------------------------------------------------------------------------- #
# 事件与状态一致（Property 14）
# --------------------------------------------------------------------------- #

def test_event_type_matches_signal_status():
    # 每周期每个受影响 group 至多 1 个事件，且事件类型与状态严格对应。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    correspondence = {
        SignalEventType.OPENED: SignalStatus.OPEN,
        SignalEventType.UPDATED: SignalStatus.SUSTAINED,
        SignalEventType.CLOSED: SignalStatus.CLOSED,
    }

    # 周期 1：开启。
    e1 = service.reconcile([_opp("g1", 0.10)])
    # 周期 2：持续。
    clock.advance(10)
    e2 = service.reconcile([_opp("g1", 0.10)])
    # 周期 3：关闭。
    clock.advance(10)
    e3 = service.reconcile([])

    for batch, group_count in ((e1, 1), (e2, 1), (e3, 1)):
        # 每周期每个受影响 group 至多一个事件。
        seen_groups = [ev.group_id for ev in batch]
        assert len(seen_groups) == len(set(seen_groups))
        assert len(batch) == group_count
        for ev in batch:
            assert correspondence[ev.event_type] is ev.status

    assert e1[0].event_type is SignalEventType.OPENED
    assert e2[0].event_type is SignalEventType.UPDATED
    assert e3[0].event_type is SignalEventType.CLOSED


# --------------------------------------------------------------------------- #
# 多 group 混合：新开 / 持续 / 关闭
# --------------------------------------------------------------------------- #

def test_mixed_groups_open_sustain_and_close():
    # 一个周期内：g1 持续、g2 关闭、g3 新开。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    # 周期 1：g1、g2 开启。
    service.reconcile([_opp("g1", 0.10), _opp("g2", 0.20)])
    clock.advance(15)
    # 周期 2：g1 仍在（持续），g2 消失（关闭），g3 新出现（开启）。
    events = service.reconcile([_opp("g1", 0.11), _opp("g3", 0.30)])

    by_group = {ev.group_id: ev for ev in events}
    # 每个受影响 group 各恰好一个事件。
    assert len(events) == 3
    assert set(by_group) == {"g1", "g2", "g3"}
    assert by_group["g1"].event_type is SignalEventType.UPDATED
    assert by_group["g2"].event_type is SignalEventType.CLOSED
    assert by_group["g3"].event_type is SignalEventType.OPENED

    # 活跃集合现在是 g1（SUSTAINED）与 g3（OPEN），g2 已移除。
    active_ids = {s.group_id: s.status for s in service.store.list_active()}
    assert active_ids == {
        "g1": SignalStatus.SUSTAINED,
        "g3": SignalStatus.OPEN,
    }


# --------------------------------------------------------------------------- #
# list_events() 累积全部历史并按发生顺序
# --------------------------------------------------------------------------- #

def test_list_events_accumulates_in_order():
    # 事件流应累积所有周期的全部事件，并保持发生顺序。
    clock = FakeClock()
    service = SignalService(store=InMemorySignalStore(), clock=clock)

    service.reconcile([_opp("g1", 0.10)])  # OPENED g1
    clock.advance(10)
    service.reconcile([_opp("g1", 0.12)])  # UPDATED g1
    clock.advance(10)
    service.reconcile([])  # CLOSED g1

    all_events = service.store.list_events()
    types = [ev.event_type for ev in all_events]
    assert types == [
        SignalEventType.OPENED,
        SignalEventType.UPDATED,
        SignalEventType.CLOSED,
    ]
    # occurred_at 单调不减，反映发生顺序。
    occurred = [ev.occurred_at for ev in all_events]
    assert occurred == sorted(occurred)
