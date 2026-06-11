"""交易持久化：订单与计划存储（Phase Three · 切片 F）。

生产交易必须可审计：每一个下单计划、每一笔订单都要可持久化、可查询、可对账。
本模块沿用信号存储（``SignalStore``）的接口化 + SQLite 风格，提供 ``TradeStore``
接口及内存与 SQLite 两种实现。订单与计划用 pydantic ``model_dump_json()`` 整体序列化，
跨重启不丢，便于事后对账与故障复盘。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

from scanner.models import Order, TradePlan


@runtime_checkable
class TradeStore(Protocol):
    """订单与交易计划的持久化接口。"""

    def upsert_plan(self, plan: TradePlan) -> None: ...
    def get_plan(self, plan_id: str) -> Optional[TradePlan]: ...
    def list_plans(self) -> List[TradePlan]: ...
    def upsert_order(self, order: Order) -> None: ...
    def get_order(self, order_id: str) -> Optional[Order]: ...
    def list_orders(self) -> List[Order]: ...


class InMemoryTradeStore:
    """内存版 :class:`TradeStore`（开发/测试）。"""

    def __init__(self) -> None:
        self._plans: Dict[str, TradePlan] = {}
        self._orders: Dict[str, Order] = {}

    def upsert_plan(self, plan: TradePlan) -> None:
        self._plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> Optional[TradePlan]:
        return self._plans.get(plan_id)

    def list_plans(self) -> List[TradePlan]:
        return list(self._plans.values())

    def upsert_order(self, order: Order) -> None:
        self._orders[order.order_id] = order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_orders(self) -> List[Order]:
        return list(self._orders.values())


class SqliteTradeStore:
    """SQLite 版 :class:`TradeStore`，订单/计划跨重启持久、可对账。

    两张表 ``trade_plans`` 与 ``orders``，各以其 id 为主键，整体以
    ``model_dump_json()`` 存于 ``payload`` 列。传 ``:memory:`` 得进程内库（测试用）。
    连接以 ``check_same_thread=False`` 创建并由一把锁串行化，适配 asyncio 跨线程访问。
    """

    def __init__(self, path: str = "trades.db") -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_plans (
                    plan_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    payload  TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def upsert_plan(self, plan: TradePlan) -> None:
        payload = plan.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trade_plans (plan_id, payload) VALUES (?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET payload = excluded.payload
                """,
                (plan.plan_id, payload),
            )
            self._conn.commit()

    def get_plan(self, plan_id: str) -> Optional[TradePlan]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM trade_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return TradePlan.model_validate_json(row["payload"]) if row else None

    def list_plans(self) -> List[TradePlan]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM trade_plans ORDER BY plan_id"
            ).fetchall()
        return [TradePlan.model_validate_json(r["payload"]) for r in rows]

    def upsert_order(self, order: Order) -> None:
        payload = order.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (order_id, payload) VALUES (?, ?)
                ON CONFLICT(order_id) DO UPDATE SET payload = excluded.payload
                """,
                (order.order_id, payload),
            )
            self._conn.commit()

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return Order.model_validate_json(row["payload"]) if row else None

    def list_orders(self) -> List[Order]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM orders ORDER BY order_id"
            ).fetchall()
        return [Order.model_validate_json(r["payload"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "TradeStore",
    "InMemoryTradeStore",
    "SqliteTradeStore",
]
