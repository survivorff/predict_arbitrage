"""交易持久化测试：TradeStore 的内存与 SQLite 实现（Phase Three · 切片 F）。

生产交易必须可审计：每个下单计划、每笔订单都要可持久化、可查询、可对账。本测试
覆盖：

- InMemoryTradeStore 与 SqliteTradeStore(":memory:") 的 upsert/get/list 往返
  （含 TradePlan 的嵌套 legs、枚举 status；Order 的枚举与可空字段）。
- SqliteTradeStore 重启恢复：临时文件写入 → close() → 同路径重开能恢复且值一致。
- upsert 覆盖语义：同 id 二次 upsert 只留最新。
- 满足 TradeStore Protocol（runtime_checkable isinstance）。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from scanner.models import (
    Order,
    OrderSide,
    OrderStatus,
    TradeLeg,
    TradePlan,
    TradePlanStatus,
)
from scanner.trade_store import (
    InMemoryTradeStore,
    SqliteTradeStore,
    TradeStore,
)

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_plan(plan_id: str = "plan-1", *, notes=None) -> TradePlan:
    """构造一个含两条嵌套 legs 的双腿计划。"""
    return TradePlan(
        plan_id=plan_id,
        group_id="grp-1",
        event_title="2024 大选结果",
        legs=[
            TradeLeg(
                platform="polymarket",
                market_id="poly-m1",
                outcome="YES",
                side=OrderSide.BUY,
                target_price=0.40,
                quantity=250.0,
                order_id="polymarket-1",
            ),
            TradeLeg(
                platform="kalshi",
                market_id="kalshi-m1",
                outcome="NO",
                side=OrderSide.BUY,
                target_price=0.45,
                quantity=222.0,
                order_id=None,  # 可空字段：未执行的腿
            ),
        ],
        expected_net_profit_margin=0.15,
        size_usd=100.0,
        status=TradePlanStatus.COMPLETED,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        notes=notes,
    )


def make_order(order_id: str = "polymarket-1", *, filled: bool = True) -> Order:
    """构造一笔订单，含枚举与可空字段（avg_fill_price/reason/platform_order_id）。"""
    if filled:
        return Order(
            order_id=order_id,
            platform="polymarket",
            market_id="poly-m1",
            outcome="YES",
            side=OrderSide.BUY,
            limit_price=0.40,
            quantity=250.0,
            status=OrderStatus.FILLED,
            platform_order_id=order_id,
            filled_quantity=250.0,
            avg_fill_price=0.40,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
            reason=None,
        )
    # 未成交：可空字段为 None、带 reason。
    return Order(
        order_id=order_id,
        platform="kalshi",
        market_id="kalshi-m1",
        outcome="NO",
        side=OrderSide.BUY,
        limit_price=0.45,
        quantity=222.0,
        status=OrderStatus.FAILED,
        platform_order_id=None,
        filled_quantity=0.0,
        avg_fill_price=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        reason="被拒：余额不足",
    )


# 两种实现都跑同一组往返测试。
def _build_inmemory() -> InMemoryTradeStore:
    return InMemoryTradeStore()


def _build_sqlite_memory() -> SqliteTradeStore:
    return SqliteTradeStore(":memory:")


STORE_BUILDERS = [_build_inmemory, _build_sqlite_memory]


# --------------------------------------------------------------------------- #
# 往返
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("build_store", STORE_BUILDERS)
def test_plan_roundtrip(build_store) -> None:
    """upsert_plan/get_plan/list_plans 往返：嵌套 legs 与枚举 status 完整保留。"""
    store = build_store()
    plan = make_plan("plan-1")
    store.upsert_plan(plan)

    got = store.get_plan("plan-1")
    assert got is not None
    assert got.plan_id == "plan-1"
    assert got.status is TradePlanStatus.COMPLETED
    assert got.size_usd == pytest.approx(100.0)
    assert len(got.legs) == 2
    # 嵌套 legs 完整还原（含枚举 side 与可空 order_id）。
    assert got.legs[0].side is OrderSide.BUY
    assert got.legs[0].order_id == "polymarket-1"
    assert got.legs[0].target_price == pytest.approx(0.40)
    assert got.legs[1].order_id is None
    assert got.legs[1].outcome == "NO"
    # 与原对象逐字段一致。
    assert got == plan

    plans = store.list_plans()
    assert len(plans) == 1
    assert plans[0] == plan
    _maybe_close(store)


@pytest.mark.parametrize("build_store", STORE_BUILDERS)
def test_order_roundtrip(build_store) -> None:
    """upsert_order/get_order/list_orders 往返：枚举与可空字段完整保留。"""
    store = build_store()
    filled = make_order("polymarket-1", filled=True)
    failed = make_order("kalshi-1", filled=False)
    store.upsert_order(filled)
    store.upsert_order(failed)

    got = store.get_order("polymarket-1")
    assert got is not None
    assert got == filled
    assert got.status is OrderStatus.FILLED
    assert got.avg_fill_price == pytest.approx(0.40)
    assert got.reason is None

    got_failed = store.get_order("kalshi-1")
    assert got_failed is not None
    assert got_failed == failed
    assert got_failed.status is OrderStatus.FAILED
    # 可空字段正确还原为 None。
    assert got_failed.avg_fill_price is None
    assert got_failed.platform_order_id is None
    assert got_failed.reason == "被拒：余额不足"

    orders = store.list_orders()
    assert len(orders) == 2
    _maybe_close(store)


@pytest.mark.parametrize("build_store", STORE_BUILDERS)
def test_get_missing_returns_none(build_store) -> None:
    """查询不存在的计划/订单返回 None；空库 list 为空。"""
    store = build_store()
    assert store.get_plan("nope") is None
    assert store.get_order("nope") is None
    assert store.list_plans() == []
    assert store.list_orders() == []
    _maybe_close(store)


@pytest.mark.parametrize("build_store", STORE_BUILDERS)
def test_upsert_overwrites_same_id(build_store) -> None:
    """同 id 二次 upsert 只留最新（覆盖语义）。"""
    store = build_store()

    store.upsert_plan(make_plan("plan-1"))
    updated = make_plan("plan-1", notes="补充备注")
    updated.status = TradePlanStatus.FAILED
    store.upsert_plan(updated)

    got = store.get_plan("plan-1")
    assert got is not None
    assert got.status is TradePlanStatus.FAILED
    assert got.notes == "补充备注"
    # 仍只有一条计划。
    assert len(store.list_plans()) == 1

    # 订单同理。
    store.upsert_order(make_order("o-1", filled=False))
    refill = make_order("o-1", filled=True)
    store.upsert_order(refill)
    got_o = store.get_order("o-1")
    assert got_o is not None
    assert got_o.status is OrderStatus.FILLED
    assert len(store.list_orders()) == 1
    _maybe_close(store)


# --------------------------------------------------------------------------- #
# SQLite 重启恢复
# --------------------------------------------------------------------------- #
def test_sqlite_persists_across_restart() -> None:
    """SqliteTradeStore：写入文件 → close() → 同路径重开能恢复且值一致。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # 让 SqliteTradeStore 自己创建，确保是全新库
    try:
        store = SqliteTradeStore(path)
        plan = make_plan("plan-persist")
        order = make_order("order-persist", filled=True)
        store.upsert_plan(plan)
        store.upsert_order(order)
        store.close()

        # 同路径重新打开，应恢复出一致的数据。
        reopened = SqliteTradeStore(path)
        got_plan = reopened.get_plan("plan-persist")
        got_order = reopened.get_order("order-persist")
        assert got_plan == plan
        assert got_order == order
        assert len(reopened.list_plans()) == 1
        assert len(reopened.list_orders()) == 1
        reopened.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


# --------------------------------------------------------------------------- #
# Protocol 一致性
# --------------------------------------------------------------------------- #
def test_implementations_satisfy_trade_store_protocol() -> None:
    """两种实现都满足 TradeStore Protocol（runtime_checkable isinstance）。"""
    assert isinstance(InMemoryTradeStore(), TradeStore)
    sqlite_store = SqliteTradeStore(":memory:")
    assert isinstance(sqlite_store, TradeStore)
    sqlite_store.close()


def _maybe_close(store) -> None:
    """SQLite 实现有 close()，内存实现没有；统一收尾。"""
    close = getattr(store, "close", None)
    if callable(close):
        close()
