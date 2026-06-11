"""执行边界：下单写接口与模拟盘实现（Phase Three · 切片 E）。

现有 :class:`~scanner.adapters.base.PlatformAdapter` 是**读边界**（拉行情）。本模块
新增对称的**写边界** :class:`ExecutionAdapter`（下单/撤单/查询余额持仓），让交易半边
与只读架构保持一致的可插拔风格。

第一阶段交易能力以 :class:`PaperExecutionAdapter`（模拟盘）打通全链路——它在内存里
模拟下单、成交、持仓与余额，**不触达任何真实平台、不动真钱**，且可注入「部分成交 /
拒单」场景以测试执行引擎的双腿原子性与补救逻辑（切片 F）。真实平台适配器
（如 Kalshi，切片 I）后续实现同一接口接入。

安全：鉴权凭证（API 密钥 / 钱包私钥）只存在于具体执行适配器内部（经环境变量注入），
绝不出现在交易域模型、日志或 API 响应中。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Protocol, runtime_checkable

from scanner.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    Position,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionError(Exception):
    """执行适配器在无法下单/撤单/查询时抛出的统一错误类型。

    与读边界的 ``AdapterError`` 对称：执行引擎是唯一决定如何处置（补救/告警）的地方，
    具体适配器把平台特定的下单失败（余额不足、被拒、网络错误）统一为 ``ExecutionError``。
    """

    def __init__(self, message: str, *, platform: Optional[str] = None) -> None:
        super().__init__(message)
        self.platform = platform


@runtime_checkable
class ExecutionAdapter(Protocol):
    """下单写边界：一个平台的交易接入契约（Phase Three）。

    具体实现承载平台特定的鉴权与下单细节；执行引擎只依赖本接口，因此新增平台
    （Kalshi / Polymarket）无需改动执行引擎与风控。

    可选属性 ``is_paper``（默认视为 False=真实）：标识该适配器是否为模拟盘（不动真钱）。
    执行引擎据此决定 dry-run 行为——dry-run 只抑制**真实**适配器，模拟盘始终照常模拟成交。
    """

    name: str

    async def place_order(
        self,
        *,
        market_id: str,
        outcome: str,
        side: OrderSide,
        limit_price: float,
        quantity: float,
    ) -> Order:
        """提交一笔限价单，返回带状态的 :class:`Order`。

        Raises:
            ExecutionError: 提交失败（余额不足、被拒、网络错误等）。
        """
        ...

    async def cancel_order(self, platform_order_id: str) -> Order:
        """撤销一笔订单，返回更新后的 :class:`Order`。"""
        ...

    async def get_order(self, platform_order_id: str) -> Order:
        """查询一笔订单的最新状态。"""
        ...

    async def get_balance(self) -> float:
        """返回可用余额（USD）。"""
        ...

    async def get_positions(self) -> List[Position]:
        """返回当前持仓列表。"""
        ...


# 成交模拟策略：给定 (限价, 数量) 返回 (成交数量, 成交均价)；用于 PaperExecutionAdapter
# 注入不同成交行为（全成/部分成交/不成交）以测试执行引擎。
FillPolicy = Callable[[float, float], "tuple[float, float]"]


def full_fill_at_limit(limit_price: float, quantity: float) -> "tuple[float, float]":
    """默认成交策略：按限价全额成交。"""
    return quantity, limit_price


@dataclass
class PaperExecutionAdapter:
    """内存模拟盘 :class:`ExecutionAdapter`（不动真钱）。

    维护一个虚拟余额与持仓集合，按 ``fill_policy`` 模拟成交。可通过
    ``reject_markets`` 注入「下单被拒」、通过自定义 ``fill_policy`` 注入「部分成交 /
    不成交」，用于测试执行引擎的双腿原子性与补救路径（切片 F）。

    Args:
        name: 适配器名（对应平台名）。
        starting_balance_usd: 初始虚拟余额。
        fill_policy: 成交策略，默认按限价全额成交。
        reject_markets: 这些 market_id 的下单会被拒（抛 ExecutionError），模拟拒单。
        clock: 注入时钟，使时间戳确定。
    """

    name: str = "paper"
    starting_balance_usd: float = 10_000.0
    fill_policy: FillPolicy = full_fill_at_limit
    reject_markets: "frozenset[str]" = field(default_factory=frozenset)
    clock: Callable[[], datetime] = _utc_now
    # 标识这是模拟盘（不动真钱）。执行引擎据此决定：dry-run 只抑制**真实**适配器，
    # 模拟盘永远照常模拟成交，使默认 dry-run 配置下用户也能看到模拟持仓/收益变化。
    is_paper: bool = True

    _balance: float = field(init=False)
    _orders: Dict[str, Order] = field(default_factory=dict, init=False)
    _positions: Dict["tuple[str, str]", Position] = field(default_factory=dict, init=False)
    _seq: "itertools.count" = field(default_factory=lambda: itertools.count(1), init=False)

    def __post_init__(self) -> None:
        self._balance = self.starting_balance_usd

    async def place_order(
        self,
        *,
        market_id: str,
        outcome: str,
        side: OrderSide,
        limit_price: float,
        quantity: float,
    ) -> Order:
        now = self.clock()
        platform_order_id = f"{self.name}-{next(self._seq)}"
        order = Order(
            order_id=platform_order_id,
            platform=self.name,
            market_id=market_id,
            outcome=outcome,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            status=OrderStatus.PENDING,
            platform_order_id=platform_order_id,
            created_at=now,
            updated_at=now,
        )

        # 注入的拒单场景。
        if market_id in self.reject_markets:
            order.status = OrderStatus.FAILED
            order.reason = "paper: market in reject list"
            self._orders[platform_order_id] = order
            raise ExecutionError(
                f"paper adapter rejected order on {market_id}", platform=self.name
            )

        filled_qty, fill_price = self.fill_policy(limit_price, quantity)
        filled_qty = max(0.0, min(filled_qty, quantity))
        cost = filled_qty * fill_price

        # 余额校验（买入）。
        if side is OrderSide.BUY and cost > self._balance:
            order.status = OrderStatus.FAILED
            order.reason = "paper: insufficient balance"
            self._orders[platform_order_id] = order
            raise ExecutionError(
                f"insufficient paper balance for order on {market_id}",
                platform=self.name,
            )

        if filled_qty <= 0:
            order.status = OrderStatus.SUBMITTED  # 已挂单但未成交
        elif filled_qty < quantity:
            order.status = OrderStatus.PARTIALLY_FILLED
        else:
            order.status = OrderStatus.FILLED

        order.filled_quantity = filled_qty
        order.avg_fill_price = fill_price if filled_qty > 0 else None
        order.updated_at = now

        if filled_qty > 0 and side is OrderSide.BUY:
            self._balance -= cost
            self._apply_fill_to_position(market_id, outcome, filled_qty, fill_price, now)

        self._orders[platform_order_id] = order
        return order

    async def cancel_order(self, platform_order_id: str) -> Order:
        order = self._orders.get(platform_order_id)
        if order is None:
            raise ExecutionError(
                f"unknown order {platform_order_id}", platform=self.name
            )
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED):
            # 已终态，撤销无效果（返回当前状态）。
            return order
        order.status = OrderStatus.CANCELLED
        order.reason = "paper: cancelled"
        order.updated_at = self.clock()
        return order

    async def get_order(self, platform_order_id: str) -> Order:
        order = self._orders.get(platform_order_id)
        if order is None:
            raise ExecutionError(
                f"unknown order {platform_order_id}", platform=self.name
            )
        return order

    async def get_balance(self) -> float:
        return self._balance

    async def get_positions(self) -> List[Position]:
        return list(self._positions.values())

    # -- 内部：成交对持仓的影响 ------------------------------------------- #
    def _apply_fill_to_position(
        self, market_id: str, outcome: str, qty: float, price: float, now: datetime
    ) -> None:
        key = (market_id, outcome)
        existing = self._positions.get(key)
        if existing is None:
            self._positions[key] = Position(
                platform=self.name,
                market_id=market_id,
                outcome=outcome,
                quantity=qty,
                avg_price=price,
                updated_at=now,
            )
        else:
            total_qty = existing.quantity + qty
            # 数量加权平均建仓价。
            avg = (
                (existing.avg_price * existing.quantity + price * qty) / total_qty
                if total_qty > 0
                else price
            )
            self._positions[key] = Position(
                platform=self.name,
                market_id=market_id,
                outcome=outcome,
                quantity=total_qty,
                avg_price=avg,
                updated_at=now,
            )


__all__ = [
    "ExecutionError",
    "ExecutionAdapter",
    "FillPolicy",
    "full_fill_at_limit",
    "PaperExecutionAdapter",
]
