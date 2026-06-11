"""交易域模型的校验测试（Phase Three · 切片 E）。

覆盖 Order / Fill / TradeLeg / TradePlan 的字段校验与默认值，以及
OrderSide / OrderStatus / TradePlanStatus 枚举成员的正确性。

只验证模型契约，不触达执行半边；时间戳用固定值以保持确定。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from scanner.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    TradeLeg,
    TradePlan,
    TradePlanStatus,
)

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #
def test_order_valid_construction() -> None:
    """正常构造一笔订单应成功，且默认值符合契约。"""
    order = Order(
        order_id="o-1",
        platform="paper",
        market_id="m-1",
        outcome="YES",
        limit_price=0.6,
        quantity=10.0,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    assert order.side is OrderSide.BUY              # 默认买入
    assert order.status is OrderStatus.PENDING      # 默认未提交
    assert order.platform_order_id is None
    assert order.filled_quantity == 0.0
    assert order.avg_fill_price is None
    assert order.reason is None


@pytest.mark.parametrize("bad_price", [-0.01, 1.01, -1.0, 2.0])
def test_order_limit_price_out_of_bounds_raises(bad_price: float) -> None:
    """limit_price 越界（<0 或 >1）应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Order(
            order_id="o-1",
            platform="paper",
            market_id="m-1",
            outcome="YES",
            limit_price=bad_price,
            quantity=10.0,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )


def test_order_negative_quantity_raises() -> None:
    """quantity 负值应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Order(
            order_id="o-1",
            platform="paper",
            market_id="m-1",
            outcome="YES",
            limit_price=0.6,
            quantity=-1.0,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )


def test_order_negative_filled_quantity_raises() -> None:
    """filled_quantity 负值应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Order(
            order_id="o-1",
            platform="paper",
            market_id="m-1",
            outcome="YES",
            limit_price=0.6,
            quantity=10.0,
            filled_quantity=-1.0,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )


# --------------------------------------------------------------------------- #
# Fill
# --------------------------------------------------------------------------- #
def test_fill_valid_construction() -> None:
    """正常构造一笔成交回报应成功。"""
    fill = Fill(
        order_id="o-1",
        platform="paper",
        quantity=5.0,
        price=0.55,
        filled_at=BASE_TIME,
    )
    assert fill.fee == 0.0  # 默认无费


@pytest.mark.parametrize("bad_price", [-0.01, 1.01])
def test_fill_price_out_of_bounds_raises(bad_price: float) -> None:
    """Fill.price 越界应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        Fill(
            order_id="o-1",
            platform="paper",
            quantity=5.0,
            price=bad_price,
            filled_at=BASE_TIME,
        )


# --------------------------------------------------------------------------- #
# TradeLeg
# --------------------------------------------------------------------------- #
def test_trade_leg_valid_construction() -> None:
    """正常构造一腿应成功，默认买入、未关联订单。"""
    leg = TradeLeg(
        platform="paper",
        market_id="m-1",
        outcome="YES",
        target_price=0.6,
        quantity=10.0,
    )
    assert leg.side is OrderSide.BUY
    assert leg.order_id is None


@pytest.mark.parametrize("bad_price", [-0.01, 1.01])
def test_trade_leg_target_price_out_of_bounds_raises(bad_price: float) -> None:
    """TradeLeg.target_price 越界应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        TradeLeg(
            platform="paper",
            market_id="m-1",
            outcome="YES",
            target_price=bad_price,
            quantity=10.0,
        )


# --------------------------------------------------------------------------- #
# TradePlan
# --------------------------------------------------------------------------- #
def test_trade_plan_valid_construction_with_two_legs() -> None:
    """正常构造一个含 2 条 legs 的计划应成功，默认待人工确认。"""
    legs = [
        TradeLeg(
            platform="kalshi",
            market_id="k-1",
            outcome="YES",
            target_price=0.4,
            quantity=10.0,
        ),
        TradeLeg(
            platform="polymarket",
            market_id="p-1",
            outcome="NO",
            target_price=0.55,
            quantity=10.0,
        ),
    ]
    plan = TradePlan(
        plan_id="plan-1",
        group_id="g-1",
        event_title="某事件",
        legs=legs,
        expected_net_profit_margin=0.05,
        size_usd=100.0,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    assert len(plan.legs) == 2
    assert plan.status is TradePlanStatus.PENDING_CONFIRMATION  # 默认待确认
    assert plan.notes is None


def test_trade_plan_negative_size_usd_raises() -> None:
    """size_usd 负值应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        TradePlan(
            plan_id="plan-1",
            group_id="g-1",
            event_title="某事件",
            legs=[],
            expected_net_profit_margin=0.05,
            size_usd=-1.0,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )


# --------------------------------------------------------------------------- #
# 枚举成员
# --------------------------------------------------------------------------- #
def test_order_side_members() -> None:
    """OrderSide 成员与取值正确。"""
    assert OrderSide.BUY.value == "buy"
    assert OrderSide.SELL.value == "sell"
    assert {s.value for s in OrderSide} == {"buy", "sell"}


def test_order_status_members() -> None:
    """OrderStatus 成员与取值正确。"""
    assert OrderStatus.PENDING.value == "pending"
    assert OrderStatus.SUBMITTED.value == "submitted"
    assert OrderStatus.FILLED.value == "filled"
    assert OrderStatus.PARTIALLY_FILLED.value == "partially_filled"
    assert OrderStatus.FAILED.value == "failed"
    assert OrderStatus.CANCELLED.value == "cancelled"
    assert {s.value for s in OrderStatus} == {
        "pending",
        "submitted",
        "filled",
        "partially_filled",
        "failed",
        "cancelled",
    }


def test_trade_plan_status_members() -> None:
    """TradePlanStatus 成员与取值正确。"""
    assert TradePlanStatus.PENDING_CONFIRMATION.value == "pending_confirmation"
    assert TradePlanStatus.CONFIRMED.value == "confirmed"
    assert TradePlanStatus.REJECTED.value == "rejected"
    assert TradePlanStatus.EXECUTING.value == "executing"
    assert TradePlanStatus.COMPLETED.value == "completed"
    assert TradePlanStatus.FAILED.value == "failed"
    assert {s.value for s in TradePlanStatus} == {
        "pending_confirmation",
        "confirmed",
        "rejected",
        "executing",
        "completed",
        "failed",
    }
