"""SqliteSignalStore 持久化测试（Phase Two · 切片 B）。

验证基于标准库 sqlite3 的 :class:`~scanner.signals.SqliteSignalStore`：

- 基本 CRUD：upsert_active 后能 get_active / list_active 取回，所有字段
  （含嵌套 legs、datetime、枚举 status、peak_net_profit_margin、closed_at）
  正确往返；remove_active 后 get_active 返回 None。
- 事件流：append_event 多次后 list_events 按插入顺序返回全部，字段正确往返。
- 重启恢复（核心）：用临时文件路径写入活跃信号与事件，close() 后用同一路径
  新建一个 store，断言活跃信号与事件流都被恢复（值一致）。
- upsert 幂等/覆盖：同一 group_id 两次 upsert，list_active 只剩一条且为最新。
- 排序稳定：list_active 按 group_id，list_events 按自增 id。
- 与 SignalService 集成：用 :memory: store 跑 reconcile 开/关信号。
- 配置选择：load_config_from_dict 解析 signal_store_path。

注意：使用临时文件的 store 在断言完成后调用 close() 释放连接再删除文件，
避免资源泄漏（macOS 下保持整洁，Windows 下避免文件占用）。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from scanner.config import load_config_from_dict
from scanner.models import (
    ArbLeg,
    ArbitrageOpportunity,
    Signal,
    SignalEvent,
    SignalEventType,
    SignalStatus,
)
from scanner.signals import SignalService, SqliteSignalStore

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟，供注入使用（参考 tests/test_ingestion.py 写法）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _signal(
    group_id: str,
    status: SignalStatus = SignalStatus.OPEN,
    *,
    net_profit_margin: float = 0.10,
    peak_net_profit_margin: float = 0.15,
    closed_at: Optional[datetime] = None,
) -> Signal:
    """构造一个带嵌套 legs、datetime、枚举状态的信号。"""
    return Signal(
        group_id=group_id,
        event_title="某事件标题",
        status=status,
        legs=[
            ArbLeg(platform="polymarket", market_id=f"{group_id}-pm", outcome="YES", price=0.4),
            ArbLeg(platform="kalshi", market_id=f"{group_id}-ks", outcome="NO", price=0.55),
        ],
        net_profit_margin=net_profit_margin,
        recommended_size_usd=250.0,
        data_age_seconds=5.0,
        first_detected_at=BASE_TIME,
        last_seen_at=BASE_TIME + timedelta(seconds=30),
        closed_at=closed_at,
        peak_net_profit_margin=peak_net_profit_margin,
    )


def _event(
    group_id: str,
    event_type: SignalEventType = SignalEventType.OPENED,
    *,
    status: SignalStatus = SignalStatus.OPEN,
    occurred_at: datetime = BASE_TIME,
) -> SignalEvent:
    """构造一个信号事件。"""
    return SignalEvent(
        event_type=event_type,
        group_id=group_id,
        event_title="某事件标题",
        status=status,
        net_profit_margin=0.10,
        recommended_size_usd=250.0,
        peak_net_profit_margin=0.15,
        duration_seconds=30.0,
        occurred_at=occurred_at,
    )


def _opp(
    group_id: str,
    net_profit_margin: float,
    *,
    event_title: str = "事件",
) -> ArbitrageOpportunity:
    """构造一个机会快照，带一条买腿。"""
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=event_title,
        legs=[
            ArbLeg(platform="polymarket", market_id=f"{group_id}-m", outcome="YES", price=0.4),
        ],
        net_profit_margin=net_profit_margin,
        recommended_size_usd=100.0,
        detected_at=BASE_TIME,
        data_age_seconds=5.0,
    )


def _assert_signals_equal(a: Signal, b: Signal) -> None:
    """逐字段断言两个信号一致（通过整体 model_dump 比较）。"""
    assert a.model_dump() == b.model_dump()


def _assert_events_equal(a: SignalEvent, b: SignalEvent) -> None:
    assert a.model_dump() == b.model_dump()


# --------------------------------------------------------------------------- #
# 基本 CRUD
# --------------------------------------------------------------------------- #

def test_upsert_and_get_active_round_trips_all_fields():
    # upsert 后 get_active 能取回，且所有字段（含嵌套 legs、datetime、枚举、峰值）正确往返。
    store = SqliteSignalStore(":memory:")
    try:
        signal = _signal("g1", SignalStatus.SUSTAINED, closed_at=None)
        store.upsert_active(signal)

        got = store.get_active("g1")
        assert got is not None
        _assert_signals_equal(got, signal)

        # 抽查关键字段的类型与值。
        assert got.status is SignalStatus.SUSTAINED
        assert got.peak_net_profit_margin == 0.15
        assert got.closed_at is None
        assert got.first_detected_at == BASE_TIME
        assert got.last_seen_at == BASE_TIME + timedelta(seconds=30)
        assert len(got.legs) == 2
        assert got.legs[0].platform == "polymarket"
        assert got.legs[1].outcome == "NO"
    finally:
        store.close()


def test_closed_at_round_trips_when_set():
    # closed_at 非空时也应正确往返（datetime + 枚举 CLOSED）。
    store = SqliteSignalStore(":memory:")
    try:
        closed_time = BASE_TIME + timedelta(seconds=90)
        signal = _signal("gc", SignalStatus.CLOSED, closed_at=closed_time)
        store.upsert_active(signal)

        got = store.get_active("gc")
        assert got is not None
        assert got.status is SignalStatus.CLOSED
        assert got.closed_at == closed_time
    finally:
        store.close()


def test_list_active_returns_all():
    # list_active 返回全部活跃信号。
    store = SqliteSignalStore(":memory:")
    try:
        store.upsert_active(_signal("g1"))
        store.upsert_active(_signal("g2"))

        active = store.list_active()
        assert {s.group_id for s in active} == {"g1", "g2"}
    finally:
        store.close()


def test_get_active_missing_returns_none():
    # 不存在的 group 返回 None。
    store = SqliteSignalStore(":memory:")
    try:
        assert store.get_active("nope") is None
    finally:
        store.close()


def test_remove_active_then_get_returns_none():
    # remove_active 后 get_active 返回 None，且从 list_active 消失。
    store = SqliteSignalStore(":memory:")
    try:
        store.upsert_active(_signal("g1"))
        store.upsert_active(_signal("g2"))

        store.remove_active("g1")
        assert store.get_active("g1") is None
        assert {s.group_id for s in store.list_active()} == {"g2"}
    finally:
        store.close()


def test_remove_active_missing_is_noop():
    # 移除不存在的 group 不报错。
    store = SqliteSignalStore(":memory:")
    try:
        store.remove_active("ghost")  # 不应抛异常
        assert store.list_active() == []
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #

def test_append_and_list_events_in_insertion_order():
    # 多次 append_event 后 list_events 按插入顺序返回全部，字段正确往返。
    store = SqliteSignalStore(":memory:")
    try:
        e1 = _event("g1", SignalEventType.OPENED, status=SignalStatus.OPEN, occurred_at=BASE_TIME)
        e2 = _event(
            "g1",
            SignalEventType.UPDATED,
            status=SignalStatus.SUSTAINED,
            occurred_at=BASE_TIME + timedelta(seconds=10),
        )
        e3 = _event(
            "g1",
            SignalEventType.CLOSED,
            status=SignalStatus.CLOSED,
            occurred_at=BASE_TIME + timedelta(seconds=20),
        )
        store.append_event(e1)
        store.append_event(e2)
        store.append_event(e3)

        events = store.list_events()
        assert [ev.event_type for ev in events] == [
            SignalEventType.OPENED,
            SignalEventType.UPDATED,
            SignalEventType.CLOSED,
        ]
        # 字段逐条往返一致。
        _assert_events_equal(events[0], e1)
        _assert_events_equal(events[1], e2)
        _assert_events_equal(events[2], e3)
    finally:
        store.close()


def test_list_events_empty_when_none_appended():
    # 无事件时 list_events 返回空列表。
    store = SqliteSignalStore(":memory:")
    try:
        assert store.list_events() == []
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 重启恢复（核心）
# --------------------------------------------------------------------------- #

def test_restart_recovers_active_signals_and_events():
    # 用临时文件路径写入活跃信号与事件，close() 后用同一路径新建 store，
    # 断言活跃信号与事件流都被恢复（值一致）。
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="signals_test_")
    os.close(fd)  # 仅需要路径，关闭文件描述符让 sqlite 自行管理
    os.remove(db_path)  # 删除空占位文件，让 SqliteSignalStore 自建

    try:
        # 第一个 store：写入活跃信号 + 事件，然后关闭释放连接。
        store1 = SqliteSignalStore(db_path)
        sig_a = _signal("g-active", SignalStatus.SUSTAINED)
        sig_b = _signal("g-open", SignalStatus.OPEN, net_profit_margin=0.20)
        store1.upsert_active(sig_a)
        store1.upsert_active(sig_b)

        ev1 = _event("g-active", SignalEventType.OPENED, status=SignalStatus.OPEN)
        ev2 = _event(
            "g-active",
            SignalEventType.UPDATED,
            status=SignalStatus.SUSTAINED,
            occurred_at=BASE_TIME + timedelta(seconds=30),
        )
        store1.append_event(ev1)
        store1.append_event(ev2)
        store1.close()

        # 第二个 store：同一路径重新打开，应恢复全部状态。
        store2 = SqliteSignalStore(db_path)
        try:
            recovered_active = {s.group_id: s for s in store2.list_active()}
            assert set(recovered_active) == {"g-active", "g-open"}
            _assert_signals_equal(recovered_active["g-active"], sig_a)
            _assert_signals_equal(recovered_active["g-open"], sig_b)
            # get_active 也应能取回单条。
            _assert_signals_equal(store2.get_active("g-open"), sig_b)

            recovered_events = store2.list_events()
            assert len(recovered_events) == 2
            _assert_events_equal(recovered_events[0], ev1)
            _assert_events_equal(recovered_events[1], ev2)
        finally:
            store2.close()
    finally:
        # 清理临时 db 文件。
        if os.path.exists(db_path):
            os.remove(db_path)


# --------------------------------------------------------------------------- #
# upsert 幂等 / 覆盖
# --------------------------------------------------------------------------- #

def test_upsert_same_group_overwrites_and_stays_single():
    # 同一 group_id 两次 upsert，list_active 只有一条且为最新值。
    store = SqliteSignalStore(":memory:")
    try:
        store.upsert_active(_signal("g1", SignalStatus.OPEN, net_profit_margin=0.10))
        store.upsert_active(
            _signal("g1", SignalStatus.SUSTAINED, net_profit_margin=0.18, peak_net_profit_margin=0.18)
        )

        active = store.list_active()
        assert len(active) == 1
        assert active[0].status is SignalStatus.SUSTAINED
        assert active[0].net_profit_margin == 0.18
        assert active[0].peak_net_profit_margin == 0.18
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 排序稳定
# --------------------------------------------------------------------------- #

def test_list_active_sorted_by_group_id():
    # list_active 按 group_id 稳定排序。
    store = SqliteSignalStore(":memory:")
    try:
        for gid in ("g3", "g1", "g2"):
            store.upsert_active(_signal(gid))
        ids = [s.group_id for s in store.list_active()]
        assert ids == ["g1", "g2", "g3"]
    finally:
        store.close()


def test_list_events_ordered_by_autoincrement_id():
    # list_events 按自增 id（插入顺序）返回，即使同一 occurred_at 也保持插入顺序。
    store = SqliteSignalStore(":memory:")
    try:
        # 故意用相同 occurred_at，验证排序依赖自增 id 而非时间戳。
        for et, st in (
            (SignalEventType.OPENED, SignalStatus.OPEN),
            (SignalEventType.UPDATED, SignalStatus.SUSTAINED),
            (SignalEventType.CLOSED, SignalStatus.CLOSED),
        ):
            store.append_event(_event("g1", et, status=st, occurred_at=BASE_TIME))

        events = store.list_events()
        assert [ev.event_type for ev in events] == [
            SignalEventType.OPENED,
            SignalEventType.UPDATED,
            SignalEventType.CLOSED,
        ]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 与 SignalService 集成
# --------------------------------------------------------------------------- #

def test_signal_service_with_sqlite_store_open_then_close():
    # SignalService 用 SQLite 存储跑一遍 reconcile 开信号、再 reconcile([]) 关信号，
    # 断言活跃集合与事件流正确（OPENED 然后 CLOSED）。
    clock = FakeClock()
    store = SqliteSignalStore(":memory:")
    try:
        service = SignalService(store=store, clock=clock)

        # 周期 1：机会出现 → 开启信号。
        opened = service.reconcile([_opp("g1", 0.10)])
        assert len(opened) == 1
        assert opened[0].event_type is SignalEventType.OPENED
        assert {s.group_id for s in store.list_active()} == {"g1"}
        assert store.get_active("g1").status is SignalStatus.OPEN

        # 周期 2：机会消失 → 关闭信号。
        clock.advance(30)
        closed = service.reconcile([])
        assert len(closed) == 1
        assert closed[0].event_type is SignalEventType.CLOSED
        # 活跃集合清空。
        assert store.list_active() == []
        assert store.get_active("g1") is None

        # 事件流应为 OPENED 然后 CLOSED。
        types = [ev.event_type for ev in store.list_events()]
        assert types == [SignalEventType.OPENED, SignalEventType.CLOSED]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 配置选择
# --------------------------------------------------------------------------- #

def test_config_signal_store_path_memory():
    # signal_store_path 可设为 :memory:。
    cfg = load_config_from_dict({"scanner": {"signal_store_path": ":memory:"}})
    assert cfg.scanner.signal_store_path == ":memory:"


def test_config_signal_store_path_defaults_none():
    # 不传时默认为 None（表示用内存存储）。
    cfg = load_config_from_dict({})
    assert cfg.scanner.signal_store_path is None
