"""信号生命周期服务与存储（Phase Two · 切片 A）。

把瞬时的 :class:`~scanner.models.ArbitrageOpportunity` 快照提升为有状态的
:class:`~scanner.models.Signal`，并在每个流水线周期对账，驱动信号在
``OPEN → SUSTAINED → CLOSED`` 之间转移，同时产出 :class:`~scanner.models.SignalEvent`
事件流——这是「信号捕捉工具」的核心产物，可被消费、回放、审计。

设计要点：

- **生命周期对账（reconcile）**：每周期接收当前可列出的机会快照；与上一周期的
  活跃信号比对：
    - 新出现的 group → 开启信号（OPEN），产出 ``OPENED`` 事件。
    - 仍存在的 group → 更新信号（SUSTAINED），刷新净利润率/规模/峰值与 ``last_seen_at``，
      产出 ``UPDATED`` 事件。
    - 本周期消失的 group → 关闭信号（CLOSED），产出 ``CLOSED`` 事件，并从活跃集合移除。
- **峰值单调**：``peak_net_profit_margin`` 在信号存活期间单调不减。
- **可注入时钟**：``clock`` 回调使时间戳与时长在测试中确定可控。
- **接口化存储**：:class:`SignalStore` 为 Protocol，本切片提供内存实现
  :class:`InMemorySignalStore`；切片 B 将无缝叠加 SQLite 持久化实现。

正确性属性（本切片新增）：

- Property 13（信号状态一致性）：一个 group 的活跃信号至多一个；信号一旦 CLOSED
  即从活跃集合移除，不再转回 OPEN/SUSTAINED（同一 group 重新出现会开启一个全新信号）。
- Property 14（事件与状态一致）：每次对账产出的事件类型与信号转移严格对应
  （OPENED↔OPEN、UPDATED↔SUSTAINED、CLOSED↔CLOSED），且每个受影响的 group 每周期
  至多产生一个事件。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Protocol, runtime_checkable

from scanner.models import (
    ArbitrageOpportunity,
    Signal,
    SignalEvent,
    SignalEventType,
    SignalStatus,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@runtime_checkable
class SignalStore(Protocol):
    """活跃信号 + 事件流的存储接口（切片 B 将提供持久化实现）。"""

    def get_active(self, group_id: str) -> Optional[Signal]:
        """返回某 group 的活跃信号，不存在则返回 None。"""
        ...

    def list_active(self) -> List[Signal]:
        """返回所有活跃信号。"""
        ...

    def upsert_active(self, signal: Signal) -> None:
        """插入或更新一个活跃信号。"""
        ...

    def remove_active(self, group_id: str) -> None:
        """从活跃集合移除某 group 的信号（信号关闭时调用）。"""
        ...

    def append_event(self, event: SignalEvent) -> None:
        """向事件流追加一个事件。"""
        ...

    def list_events(self) -> List[SignalEvent]:
        """返回按发生顺序排列的全部事件。"""
        ...


class InMemorySignalStore:
    """内存版 :class:`SignalStore`：活跃信号用 dict，事件流用 list 追加。

    事件流设保留上限 ``max_events``（默认 50000），超出时丢弃最旧的，防止长跑进程
    内存无限增长。``list_events`` 仍按发生顺序返回保留窗口内的事件。
    """

    def __init__(self, *, max_events: int = 50_000) -> None:
        self._active: Dict[str, Signal] = {}
        self._events: List[SignalEvent] = []
        self._max_events = max(1, max_events)

    def get_active(self, group_id: str) -> Optional[Signal]:
        return self._active.get(group_id)

    def list_active(self) -> List[Signal]:
        return list(self._active.values())

    def upsert_active(self, signal: Signal) -> None:
        self._active[signal.group_id] = signal

    def remove_active(self, group_id: str) -> None:
        self._active.pop(group_id, None)

    def append_event(self, event: SignalEvent) -> None:
        self._events.append(event)
        # 超出保留窗口时裁剪最旧事件（防内存无限增长）。
        if len(self._events) > self._max_events:
            overflow = len(self._events) - self._max_events
            del self._events[:overflow]

    def list_events(self) -> List[SignalEvent]:
        return list(self._events)


class SqliteSignalStore:
    """基于 SQLite 的 :class:`SignalStore` 持久化实现（Phase Two · 切片 B）。

    用 Python 标准库的 ``sqlite3``，零外部依赖、可文件落盘。两张表：

    - ``active_signals`` —— 活跃信号快照，以 ``group_id`` 为主键，整条信号以
      ``model_dump_json()`` 存于 ``payload`` 列；信号关闭时该行被删除。启动时活跃
      信号天然就在表中，因此「重启恢复」无需额外逻辑——调用方零改动。
    - ``signal_events`` —— 事件流，只追加，自增 ``id`` 保证回放/审计的发生顺序。

    传 ``path=":memory:"`` 可得到进程内数据库（测试用）。所有写操作即时 ``commit``，
    使信号在进程崩溃后仍可恢复。SQLite 连接以 ``check_same_thread=False`` 创建并由
    一把锁串行化，以适配 asyncio 流水线可能跨线程的访问。
    """

    def __init__(self, path: str = "signals.db", *, max_events: int = 200_000) -> None:
        self._path = path
        self._max_events = max(1, max_events)
        self._append_count = 0
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS active_signals (
                    group_id TEXT PRIMARY KEY,
                    payload  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    payload  TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def get_active(self, group_id: str) -> Optional[Signal]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM active_signals WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return None
        return Signal.model_validate_json(row["payload"])

    def list_active(self) -> List[Signal]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM active_signals ORDER BY group_id"
            ).fetchall()
        return [Signal.model_validate_json(r["payload"]) for r in rows]

    def upsert_active(self, signal: Signal) -> None:
        payload = signal.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO active_signals (group_id, payload) VALUES (?, ?)
                ON CONFLICT(group_id) DO UPDATE SET payload = excluded.payload
                """,
                (signal.group_id, payload),
            )
            self._conn.commit()

    def remove_active(self, group_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM active_signals WHERE group_id = ?", (group_id,)
            )
            self._conn.commit()

    def append_event(self, event: SignalEvent) -> None:
        payload = event.model_dump_json()
        with self._lock:
            self._conn.execute(
                "INSERT INTO signal_events (group_id, payload) VALUES (?, ?)",
                (event.group_id, payload),
            )
            self._conn.commit()
            # 周期性裁剪：每追加若干条检查一次，删除超出保留窗口的最旧事件，
            # 防止 signal_events 表无限增长（机会性清理，避免每次都全表扫描）。
            self._append_count += 1
            if self._append_count % 500 == 0:
                self._conn.execute(
                    """
                    DELETE FROM signal_events WHERE id NOT IN (
                        SELECT id FROM signal_events ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (self._max_events,),
                )
                self._conn.commit()

    def list_events(self) -> List[SignalEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM signal_events ORDER BY id"
            ).fetchall()
        return [SignalEvent.model_validate_json(r["payload"]) for r in rows]

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        with self._lock:
            self._conn.close()


@dataclass
class SignalService:
    """把机会快照对账为有状态的信号，并产出信号事件流。

    Args:
        store: 信号存储。默认新建一个内存 ``InMemorySignalStore``。
        clock: 返回当前 UTC 时间的回调，注入以保证确定性。
    """

    store: SignalStore = field(default_factory=InMemorySignalStore)
    clock: callable = _utc_now

    def reconcile(
        self, opportunities: Iterable[ArbitrageOpportunity]
    ) -> List[SignalEvent]:
        """用当前机会快照对账信号状态，返回本周期产生的事件。

        ``opportunities`` 应为已过滤的「可列出」机会（净利润率 > 0 且达阈值），
        与 :class:`~scanner.opportunities.OpportunityService` 的输出一致。
        """
        now = self.clock()
        opps_by_group = {opp.group_id: opp for opp in opportunities}
        present_ids = set(opps_by_group)
        active_ids = {s.group_id for s in self.store.list_active()}

        events: List[SignalEvent] = []

        # 1) 新出现 / 持续存在的 group。
        for group_id, opp in opps_by_group.items():
            existing = self.store.get_active(group_id)
            if existing is None:
                signal, event = self._open_signal(opp, now)
            else:
                signal, event = self._update_signal(existing, opp, now)
            self.store.upsert_active(signal)
            self.store.append_event(event)
            events.append(event)

        # 2) 本周期消失的 group → 关闭信号。
        for group_id in active_ids - present_ids:
            existing = self.store.get_active(group_id)
            if existing is None:
                continue
            signal, event = self._close_signal(existing, now)
            self.store.remove_active(group_id)
            self.store.append_event(event)
            events.append(event)

        return events

    # -- 状态转移 ----------------------------------------------------------

    def _open_signal(self, opp: ArbitrageOpportunity, now: datetime):
        signal = Signal(
            group_id=opp.group_id,
            event_title=opp.event_title,
            status=SignalStatus.OPEN,
            legs=list(opp.legs),
            net_profit_margin=opp.net_profit_margin,
            recommended_size_usd=opp.recommended_size_usd,
            data_age_seconds=opp.data_age_seconds,
            first_detected_at=now,
            last_seen_at=now,
            peak_net_profit_margin=opp.net_profit_margin,
        )
        return signal, self._event(SignalEventType.OPENED, signal, now)

    def _update_signal(self, prev: Signal, opp: ArbitrageOpportunity, now: datetime):
        signal = Signal(
            group_id=prev.group_id,
            event_title=opp.event_title,
            status=SignalStatus.SUSTAINED,
            legs=list(opp.legs),
            net_profit_margin=opp.net_profit_margin,
            recommended_size_usd=opp.recommended_size_usd,
            data_age_seconds=opp.data_age_seconds,
            first_detected_at=prev.first_detected_at,
            last_seen_at=now,
            # 峰值单调不减。
            peak_net_profit_margin=max(
                prev.peak_net_profit_margin, opp.net_profit_margin
            ),
        )
        return signal, self._event(SignalEventType.UPDATED, signal, now)

    def _close_signal(self, prev: Signal, now: datetime):
        signal = Signal(
            group_id=prev.group_id,
            event_title=prev.event_title,
            status=SignalStatus.CLOSED,
            legs=list(prev.legs),
            net_profit_margin=prev.net_profit_margin,
            recommended_size_usd=prev.recommended_size_usd,
            data_age_seconds=prev.data_age_seconds,
            first_detected_at=prev.first_detected_at,
            last_seen_at=prev.last_seen_at,
            closed_at=now,
            peak_net_profit_margin=prev.peak_net_profit_margin,
        )
        return signal, self._event(SignalEventType.CLOSED, signal, now)

    @staticmethod
    def _event(
        event_type: SignalEventType, signal: Signal, now: datetime
    ) -> SignalEvent:
        return SignalEvent(
            event_type=event_type,
            group_id=signal.group_id,
            event_title=signal.event_title,
            status=signal.status,
            net_profit_margin=signal.net_profit_margin,
            recommended_size_usd=signal.recommended_size_usd,
            peak_net_profit_margin=signal.peak_net_profit_margin,
            duration_seconds=signal.duration_seconds,
            occurred_at=now,
        )


__all__ = [
    "SignalStore",
    "InMemorySignalStore",
    "SqliteSignalStore",
    "SignalService",
]
