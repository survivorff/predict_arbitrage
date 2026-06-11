"""行情/机会历史时间序列持久化测试（Phase 3 · 切片 K）。

验证 :mod:`scanner.history`：

- ``record_opportunities`` 后 ``opportunity_series`` 取回对应点：value=净利润率、
  label=事件标题、按时间升序（用 FakeClock 注入不同时间戳多次记录）。
- ``record_markets``：YES 价被记录；无 YES 取第一个 outcome；无 outcome 的市场被跳过。
- ``limit`` 生效：记录超过 limit 个点，series 只返回最近 limit 个（仍升序）。
- 保留窗口 ``_prune``：t0 记录后 advance 超过 retention_days 再记录，断言旧点被删。
- ``NullHistoryStore``：record 不报错、series 返回 []，且满足 HistoryStore Protocol。
- ``SqliteHistoryStore`` 也满足 Protocol；:memory: 与临时文件两种都测。
- 重启恢复（临时文件）：写入→close()→同路径重开→series 仍能取回。

注意：临时文件 store 断言完成后 close() 并清理文件，避免资源泄漏。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from scanner.history import (
    HistoryPoint,
    HistoryStore,
    NullHistoryStore,
    SqliteHistoryStore,
)
from scanner.models import ArbLeg, ArbitrageOpportunity, CanonicalMarket, Outcome

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟（参考 tests/test_signals_sqlite.py 写法）。"""

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
    event_title: str = "某事件标题",
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


def _market(
    platform: str,
    market_id: str,
    *,
    title: str = "某市场",
    outcomes: Optional[List[Outcome]] = None,
) -> CanonicalMarket:
    """构造一个市场快照。"""
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=outcomes if outcomes is not None else [],
        retrieved_at=BASE_TIME,
    )


# --------------------------------------------------------------------------- #
# record_opportunities / opportunity_series
# --------------------------------------------------------------------------- #

def test_opportunity_series_round_trips_and_is_time_ascending():
    # 用 FakeClock 注入不同时间戳记录多次，断言取回点的 value/label 正确且按时间升序。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        store.record_opportunities([_opp("g1", 0.10, event_title="事件A")])
        clock.advance(60)
        store.record_opportunities([_opp("g1", 0.15, event_title="事件A")])
        clock.advance(60)
        store.record_opportunities([_opp("g1", 0.12, event_title="事件A")])

        series = store.opportunity_series("g1")
        assert len(series) == 3
        # value = net_profit_margin，按时间升序。
        assert [p.value for p in series] == [0.10, 0.15, 0.12]
        # label = event_title。
        assert all(p.label == "事件A" for p in series)
        # key = group_id。
        assert all(p.key == "g1" for p in series)
        # 时间戳严格升序，且与注入时钟一致。
        ats = [p.at for p in series]
        assert ats == sorted(ats)
        assert ats[0] == BASE_TIME
        assert ats[1] == BASE_TIME + timedelta(seconds=60)
        assert ats[2] == BASE_TIME + timedelta(seconds=120)
    finally:
        store.close()


def test_opportunity_series_filters_by_group_id():
    # 不同 group 的点互不串台。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        store.record_opportunities([_opp("g1", 0.10), _opp("g2", 0.20)])
        clock.advance(30)
        store.record_opportunities([_opp("g1", 0.11)])

        s1 = store.opportunity_series("g1")
        s2 = store.opportunity_series("g2")
        assert [p.value for p in s1] == [0.10, 0.11]
        assert [p.value for p in s2] == [0.20]
        assert store.opportunity_series("不存在") == []
    finally:
        store.close()


def test_opportunity_series_explicit_at_overrides_clock():
    # 显式传入 at 时间戳应优先于时钟。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        explicit = BASE_TIME + timedelta(hours=1)
        store.record_opportunities([_opp("g1", 0.10)], at=explicit)
        series = store.opportunity_series("g1")
        assert len(series) == 1
        assert series[0].at == explicit
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# record_markets / market_series
# --------------------------------------------------------------------------- #

def test_market_series_records_yes_price():
    # 有 YES outcome 时记录 YES 价。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        m = _market(
            "polymarket",
            "p1",
            title="市场甲",
            outcomes=[Outcome(name="NO", price=0.6), Outcome(name="YES", price=0.4)],
        )
        store.record_markets([m])
        clock.advance(60)
        m2 = _market(
            "polymarket",
            "p1",
            title="市场甲",
            outcomes=[Outcome(name="NO", price=0.55), Outcome(name="YES", price=0.45)],
        )
        store.record_markets([m2])

        series = store.market_series("polymarket", "p1")
        assert [p.value for p in series] == [0.4, 0.45]  # 升序，取 YES 价
        assert all(p.label == "市场甲" for p in series)
        assert all(p.key == "polymarket:p1" for p in series)
        ats = [p.at for p in series]
        assert ats == sorted(ats)
    finally:
        store.close()


def test_market_series_falls_back_to_first_outcome_when_no_yes():
    # 无 YES outcome 时取第一个 outcome 的 price。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        m = _market(
            "kalshi",
            "k1",
            outcomes=[Outcome(name="蓝队", price=0.3), Outcome(name="红队", price=0.7)],
        )
        store.record_markets([m])
        series = store.market_series("kalshi", "k1")
        assert len(series) == 1
        assert series[0].value == 0.3  # 第一个 outcome
    finally:
        store.close()


def test_market_without_outcomes_is_skipped():
    # 无 outcome 的市场被跳过：不报错、不产生行。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        empty = _market("polymarket", "empty", outcomes=[])
        good = _market(
            "polymarket",
            "good",
            outcomes=[Outcome(name="YES", price=0.5)],
        )
        # 不应抛异常。
        store.record_markets([empty, good])

        assert store.market_series("polymarket", "empty") == []
        assert [p.value for p in store.market_series("polymarket", "good")] == [0.5]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# limit
# --------------------------------------------------------------------------- #

def test_opportunity_series_limit_returns_most_recent_ascending():
    # 记录超过 limit 个点，series 只返回最近 limit 个，且仍按时间升序。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        for i in range(5):
            store.record_opportunities([_opp("g1", float(i) / 100.0)])
            clock.advance(60)

        series = store.opportunity_series("g1", limit=3)
        assert len(series) == 3
        # 最近 3 个为 i=2,3,4，升序排列。
        assert [round(p.value, 4) for p in series] == [0.02, 0.03, 0.04]
        ats = [p.at for p in series]
        assert ats == sorted(ats)
    finally:
        store.close()


def test_market_series_limit_returns_most_recent_ascending():
    # market_series 的 limit 同样生效。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", clock=clock)
    try:
        for i in range(4):
            m = _market(
                "polymarket",
                "p1",
                outcomes=[Outcome(name="YES", price=0.1 * (i + 1))],
            )
            store.record_markets([m])
            clock.advance(60)

        series = store.market_series("polymarket", "p1", limit=2)
        assert len(series) == 2
        # 最近两个为 0.3、0.4。
        assert [round(p.value, 4) for p in series] == [0.3, 0.4]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 保留窗口 _prune
# --------------------------------------------------------------------------- #

def test_retention_prunes_old_opportunity_points():
    # retention_days=1，t0 记录后 advance 2 天再记录新点，断言旧点被删除。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", retention_days=1.0, clock=clock)
    try:
        store.record_opportunities([_opp("g1", 0.10)])  # t0
        assert len(store.opportunity_series("g1")) == 1

        clock.advance(2 * 24 * 3600)  # 前进 2 天，超过 1 天保留窗口
        store.record_opportunities([_opp("g1", 0.20)])  # 触发 _prune

        series = store.opportunity_series("g1")
        # t0 的点应被裁剪，只剩新点。
        assert len(series) == 1
        assert series[0].value == 0.20
        assert series[0].at == BASE_TIME + timedelta(days=2)
    finally:
        store.close()


def test_retention_keeps_points_within_window():
    # 保留窗口内的旧点不应被删除。
    clock = FakeClock()
    store = SqliteHistoryStore(":memory:", retention_days=7.0, clock=clock)
    try:
        store.record_opportunities([_opp("g1", 0.10)])  # t0
        clock.advance(2 * 24 * 3600)  # 仅前进 2 天，仍在 7 天窗口内
        store.record_opportunities([_opp("g1", 0.20)])

        series = store.opportunity_series("g1")
        assert [p.value for p in series] == [0.10, 0.20]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# NullHistoryStore 与 Protocol
# --------------------------------------------------------------------------- #

def test_null_history_store_is_noop_and_returns_empty():
    # record 不报错，series 返回 []。
    store = NullHistoryStore()
    store.record_opportunities([_opp("g1", 0.10)])
    store.record_markets([_market("polymarket", "p1", outcomes=[Outcome(name="YES", price=0.5)])])
    assert store.opportunity_series("g1") == []
    assert store.market_series("polymarket", "p1") == []


def test_stores_satisfy_history_store_protocol():
    # NullHistoryStore 与 SqliteHistoryStore 都应满足 HistoryStore Protocol（runtime_checkable）。
    assert isinstance(NullHistoryStore(), HistoryStore)
    store = SqliteHistoryStore(":memory:")
    try:
        assert isinstance(store, HistoryStore)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 临时文件 + 重启恢复
# --------------------------------------------------------------------------- #

def test_sqlite_history_store_with_temp_file_round_trips():
    # 临时文件存储也能正常记录与取回；断言后 close() 并清理。
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="history_test_")
    os.close(fd)
    os.remove(db_path)  # 删除空占位文件，让 SqliteHistoryStore 自建

    clock = FakeClock()
    store = SqliteHistoryStore(db_path, clock=clock)
    try:
        store.record_opportunities([_opp("g1", 0.10)])
        clock.advance(60)
        store.record_opportunities([_opp("g1", 0.12)])
        series = store.opportunity_series("g1")
        assert [p.value for p in series] == [0.10, 0.12]
    finally:
        store.close()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_restart_recovers_history_within_retention():
    # 写入→close()→同路径重开→series 仍能取回（在保留窗口内）。
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="history_restart_")
    os.close(fd)
    os.remove(db_path)

    clock = FakeClock()
    try:
        store1 = SqliteHistoryStore(db_path, retention_days=7.0, clock=clock)
        store1.record_opportunities([_opp("g1", 0.10, event_title="事件A")])
        clock.advance(60)
        store1.record_opportunities([_opp("g1", 0.15, event_title="事件A")])
        store1.record_markets(
            [_market("polymarket", "p1", title="市场甲", outcomes=[Outcome(name="YES", price=0.4)])]
        )
        store1.close()

        # 同路径重开，应恢复历史（时钟仍在保留窗口内）。
        store2 = SqliteHistoryStore(db_path, retention_days=7.0, clock=clock)
        try:
            opp_series = store2.opportunity_series("g1")
            assert [p.value for p in opp_series] == [0.10, 0.15]
            assert all(p.label == "事件A" for p in opp_series)

            mkt_series = store2.market_series("polymarket", "p1")
            assert [p.value for p in mkt_series] == [0.4]
            assert mkt_series[0].label == "市场甲"
        finally:
            store2.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_history_point_is_dataclass_with_expected_fields():
    # HistoryPoint 字段齐全（key/label/value/at）。
    point = HistoryPoint(key="g1", label="事件", value=0.1, at=BASE_TIME)
    assert point.key == "g1"
    assert point.label == "事件"
    assert point.value == 0.1
    assert point.at == BASE_TIME
