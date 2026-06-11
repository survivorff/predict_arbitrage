"""行情/机会历史时间序列持久化（Phase 3 · 切片 K）。

只读快照（行情、机会）本身是瞬时的、放内存即可，但**历史时间序列**有产品价值：
画某个套利机会的净利润率走势、回看信号触发时的价差。本模块用 SQLite（复用既有存储
模式，零外部依赖）记录每个检测周期的轻量采样点，并设保留窗口防止无限膨胀。

记录两类点（都很轻量，每周期每组/每市场一行）：
- ``opportunity_history``：每个被评估机会组的 ``net_profit_margin`` 时间点 —— 画价差/利润率走势。
- ``market_price_history``：每个市场 YES 价的时间点 —— 回看价格变动。

保留策略：按时间窗口（默认 7 天）裁剪；写入时机会性地清理过期行。接口化以便将来换
时序库（如 TimescaleDB / InfluxDB）。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Protocol, runtime_checkable

from scanner.models import ArbitrageOpportunity, CanonicalMarket


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HistoryPoint:
    """一个时间序列采样点（通用：值 + 时间戳 + 标识）。"""

    key: str            # group_id 或 platform:market_id
    label: str          # 事件标题或市场标题（便于展示）
    value: float        # net_profit_margin 或 price
    at: datetime


@runtime_checkable
class HistoryStore(Protocol):
    """行情/机会历史时间序列存储接口。"""

    def record_opportunities(
        self, opportunities: List[ArbitrageOpportunity], *, at: Optional[datetime] = None
    ) -> None: ...

    def record_markets(
        self, markets: List[CanonicalMarket], *, at: Optional[datetime] = None
    ) -> None: ...

    def opportunity_series(self, group_id: str, *, limit: int = 500) -> List[HistoryPoint]: ...

    def market_series(self, platform: str, market_id: str, *, limit: int = 500) -> List[HistoryPoint]: ...


class NullHistoryStore:
    """不记录任何历史的空实现（默认/关闭历史时使用）。"""

    def record_opportunities(self, opportunities, *, at=None) -> None:  # noqa: D401
        return None

    def record_markets(self, markets, *, at=None) -> None:
        return None

    def opportunity_series(self, group_id, *, limit=500) -> List[HistoryPoint]:
        return []

    def market_series(self, platform, market_id, *, limit=500) -> List[HistoryPoint]:
        return []


class SqliteHistoryStore:
    """SQLite 历史时间序列存储，带保留窗口裁剪（Phase 3 · 切片 K）。

    Args:
        path: SQLite 文件路径（``:memory:`` 用于测试）。
        retention_days: 保留窗口（天）；写入时机会性删除更早的行。
        clock: 注入时钟，使时间戳/裁剪确定。
    """

    def __init__(
        self,
        path: str = "history.db",
        *,
        retention_days: float = 7.0,
        clock=_utc_now,
    ) -> None:
        self._path = path
        self._retention = timedelta(days=retention_days)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS opportunity_history (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    label    TEXT NOT NULL,
                    margin   REAL NOT NULL,
                    ts       TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_opp_hist_group
                    ON opportunity_history(group_id, ts);
                CREATE TABLE IF NOT EXISTS market_price_history (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform  TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    label     TEXT NOT NULL,
                    price     REAL NOT NULL,
                    ts        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mkt_hist_key
                    ON market_price_history(platform, market_id, ts);
                """
            )
            self._conn.commit()

    def record_opportunities(
        self, opportunities: List[ArbitrageOpportunity], *, at: Optional[datetime] = None
    ) -> None:
        now = at or self._clock()
        iso = now.isoformat()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO opportunity_history (group_id, label, margin, ts) VALUES (?, ?, ?, ?)",
                [(o.group_id, o.event_title, o.net_profit_margin, iso) for o in opportunities],
            )
            self._conn.commit()
        self._prune(now)

    def record_markets(
        self, markets: List[CanonicalMarket], *, at: Optional[datetime] = None
    ) -> None:
        now = at or self._clock()
        iso = now.isoformat()
        rows = []
        for m in markets:
            yes = next((o for o in m.outcomes if o.name.upper() == "YES"), None)
            price = yes.price if yes is not None else (m.outcomes[0].price if m.outcomes else None)
            if price is None:
                continue
            rows.append((m.platform, m.market_id, m.title, price, iso))
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO market_price_history (platform, market_id, label, price, ts) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def opportunity_series(self, group_id: str, *, limit: int = 500) -> List[HistoryPoint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, label, margin, ts FROM opportunity_history "
                "WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        # 反转为时间升序，便于直接画图。
        return [
            HistoryPoint(key=r["group_id"], label=r["label"], value=r["margin"],
                         at=datetime.fromisoformat(r["ts"]))
            for r in reversed(rows)
        ]

    def market_series(self, platform: str, market_id: str, *, limit: int = 500) -> List[HistoryPoint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, market_id, label, price, ts FROM market_price_history "
                "WHERE platform = ? AND market_id = ? ORDER BY id DESC LIMIT ?",
                (platform, market_id, limit),
            ).fetchall()
        return [
            HistoryPoint(key=f"{r['platform']}:{r['market_id']}", label=r["label"],
                         value=r["price"], at=datetime.fromisoformat(r["ts"]))
            for r in reversed(rows)
        ]

    def _prune(self, now: datetime) -> None:
        """删除超出保留窗口的历史行（机会性清理，控制库膨胀）。"""
        cutoff = (now - self._retention).isoformat()
        with self._lock:
            self._conn.execute("DELETE FROM opportunity_history WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM market_price_history WHERE ts < ?", (cutoff,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "HistoryPoint",
    "HistoryStore",
    "NullHistoryStore",
    "SqliteHistoryStore",
]
