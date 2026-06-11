"""PaperExecutionAdapter（内存模拟盘）行为测试（Phase Three · 切片 E）。

通过注入 FakeClock 与自定义 fill_policy，确定性地覆盖：全额成交、加权持仓、
部分成交、不成交、拒单、余额不足、撤单、未知订单查询、订单号递增、时钟注入。

asyncio_mode=auto，故 async 测试无需显式标记。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple

import pytest

from scanner.execution import (
    ExecutionAdapter,
    ExecutionError,
    PaperExecutionAdapter,
)
from scanner.models import OrderSide, OrderStatus

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟，供注入使用（参考 tests/test_ingestion.py 写法）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# Protocol 一致性
# --------------------------------------------------------------------------- #
def test_satisfies_execution_adapter_protocol() -> None:
    """PaperExecutionAdapter 实例应满足 ExecutionAdapter Protocol（runtime_checkable）。"""
    adapter = PaperExecutionAdapter()
    assert isinstance(adapter, ExecutionAdapter)


# --------------------------------------------------------------------------- #
# 成交行为
# --------------------------------------------------------------------------- #
async def test_full_fill_default_policy() -> None:
    """默认策略全额成交：状态 FILLED、数量/均价正确、余额扣减、持仓出现。"""
    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 10.0
    assert order.avg_fill_price == 0.6

    # 余额扣减 = qty * price = 10 * 0.6 = 6.0
    assert await adapter.get_balance() == pytest.approx(1000.0 - 6.0)

    positions = await adapter.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.market_id == "m-1"
    assert pos.outcome == "YES"
    assert pos.quantity == 10.0
    assert pos.avg_price == pytest.approx(0.6)


async def test_multiple_fills_same_market_outcome_weighted_average() -> None:
    """同一 (market_id, outcome) 多次成交：数量累加、均价为数量加权平均。"""
    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0)
    await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.4,
        quantity=10.0,
    )
    await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=30.0,
    )
    positions = await adapter.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.quantity == 40.0
    # 加权均价 = (0.4*10 + 0.6*30) / 40 = (4 + 18) / 40 = 0.55
    assert pos.avg_price == pytest.approx(0.55)


async def test_partial_fill() -> None:
    """注入部分成交策略：状态 PARTIALLY_FILLED、filled_quantity 为部分量。"""

    def partial(limit_price: float, quantity: float) -> Tuple[float, float]:
        return quantity / 2.0, limit_price

    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0, fill_policy=partial)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 5.0
    assert order.avg_fill_price == 0.6


async def test_no_fill() -> None:
    """注入不成交策略：状态 SUBMITTED、无持仓、余额不变。"""

    def no_fill(limit_price: float, quantity: float) -> Tuple[float, float]:
        return 0.0, limit_price

    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0, fill_policy=no_fill)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_quantity == 0.0
    assert order.avg_fill_price is None
    assert await adapter.get_positions() == []
    assert await adapter.get_balance() == 1000.0


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
async def test_reject_market_raises_and_records_failed_order() -> None:
    """reject_markets 含该 market：抛 ExecutionError，且订单可经 get_order 查到 FAILED。"""
    adapter = PaperExecutionAdapter(reject_markets=frozenset({"m-bad"}))
    with pytest.raises(ExecutionError):
        await adapter.place_order(
            market_id="m-bad",
            outcome="YES",
            side=OrderSide.BUY,
            limit_price=0.6,
            quantity=10.0,
        )
    # 平台订单号为递增首个 "paper-1"。
    order = await adapter.get_order("paper-1")
    assert order.status is OrderStatus.FAILED


async def test_insufficient_balance_raises_and_balance_unchanged() -> None:
    """余额不足：抛 ExecutionError、订单 FAILED、余额不变。"""
    adapter = PaperExecutionAdapter(starting_balance_usd=1.0)
    with pytest.raises(ExecutionError):
        await adapter.place_order(
            market_id="m-1",
            outcome="YES",
            side=OrderSide.BUY,
            limit_price=0.6,
            quantity=10.0,  # 成本 6.0 > 余额 1.0
        )
    order = await adapter.get_order("paper-1")
    assert order.status is OrderStatus.FAILED
    assert await adapter.get_balance() == 1.0
    assert await adapter.get_positions() == []


# --------------------------------------------------------------------------- #
# 撤单与查询
# --------------------------------------------------------------------------- #
async def test_cancel_submitted_order() -> None:
    """对未成交（SUBMITTED）订单撤销：状态变为 CANCELLED。"""

    def no_fill(limit_price: float, quantity: float) -> Tuple[float, float]:
        return 0.0, limit_price

    adapter = PaperExecutionAdapter(fill_policy=no_fill)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.status is OrderStatus.SUBMITTED
    cancelled = await adapter.cancel_order(order.platform_order_id)
    assert cancelled.status is OrderStatus.CANCELLED


async def test_cancel_filled_order_returns_as_is() -> None:
    """对已 FILLED 订单撤销：原样返回 FILLED，不报错。"""
    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.status is OrderStatus.FILLED
    result = await adapter.cancel_order(order.platform_order_id)
    assert result.status is OrderStatus.FILLED


async def test_cancel_unknown_order_raises() -> None:
    """撤销未知订单：抛 ExecutionError。"""
    adapter = PaperExecutionAdapter()
    with pytest.raises(ExecutionError):
        await adapter.cancel_order("does-not-exist")


async def test_get_unknown_order_raises() -> None:
    """查询未知订单：抛 ExecutionError。"""
    adapter = PaperExecutionAdapter()
    with pytest.raises(ExecutionError):
        await adapter.get_order("does-not-exist")


# --------------------------------------------------------------------------- #
# 订单号与时钟
# --------------------------------------------------------------------------- #
async def test_platform_order_id_is_incrementing_and_unique() -> None:
    """platform_order_id 递增唯一（paper-1, paper-2, ...）。"""
    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0)
    o1 = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.1,
        quantity=1.0,
    )
    o2 = await adapter.place_order(
        market_id="m-2",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.1,
        quantity=1.0,
    )
    assert o1.platform_order_id == "paper-1"
    assert o2.platform_order_id == "paper-2"
    assert o1.platform_order_id != o2.platform_order_id


async def test_injected_clock_sets_timestamps() -> None:
    """注入时钟：created_at / updated_at 等于注入时钟值。"""
    clock = FakeClock()
    adapter = PaperExecutionAdapter(starting_balance_usd=1000.0, clock=clock)
    order = await adapter.place_order(
        market_id="m-1",
        outcome="YES",
        side=OrderSide.BUY,
        limit_price=0.6,
        quantity=10.0,
    )
    assert order.created_at == BASE_TIME
    assert order.updated_at == BASE_TIME
