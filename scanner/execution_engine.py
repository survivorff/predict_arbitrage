"""套利执行引擎与双腿原子性（Phase Three · 切片 F）。

本模块是交易系统**风险最高**的部分：把一个 :class:`~scanner.models.ArbitrageOpportunity`
落成双腿 :class:`~scanner.models.TradePlan` 并执行。套利的头号风险是**双腿原子性**——
若一腿成交、另一腿失败，就留下单边裸敞口，可能巨亏。引擎的核心职责就是**绝不放任
单边裸敞口**：任一腿失败时，尝试撤销/平掉已成交的腿，并把残余敞口明确记录与告警。

执行流程（``execute_plan``）：
1. 计划状态置 ``EXECUTING``。
2. 按腿依次下单（通过对应平台的 :class:`~scanner.execution.ExecutionAdapter`）。
3. 全部腿成交 → ``COMPLETED``。
4. 任一腿失败/未成交 → 进入**补救**：对已成交的腿尝试反向平仓（卖出）或撤销未成交腿；
   计划置 ``FAILED``，在 ``notes`` 记录补救动作与任何残余敞口，供告警与人工介入。

设计取向：
- 引擎只依赖 ``ExecutionAdapter`` 接口与交易域模型，不感知具体平台。
- 注入 ``clock`` 使时间戳确定；所有路径（全成/一腿失败/补救成功/补救失败）确定可测。
- 订单与计划的持久化交由 :class:`~scanner.trade_store.TradeStore`（见切片 F 存储部分）。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from scanner.execution import ExecutionAdapter, ExecutionError
from scanner.models import (
    ArbitrageOpportunity,
    Order,
    OrderSide,
    OrderStatus,
    TradeLeg,
    TradePlan,
    TradePlanStatus,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExecutionEngine:
    """把套利机会转成双腿计划并执行，保证双腿原子性（Phase Three · 切片 F）。

    Args:
        adapters: 平台名 -> :class:`ExecutionAdapter` 的映射。
        clock: 注入时钟，使时间戳确定。
    """

    adapters: Dict[str, ExecutionAdapter]
    clock: Callable[[], datetime] = _utc_now
    _plan_seq: "itertools.count" = field(
        default_factory=lambda: itertools.count(1), init=False
    )
    # 最近一次执行各计划产生的已成交订单（plan_id -> 订单列表），供调用方持久化对账。
    last_orders: Dict[str, List["Order"]] = field(default_factory=dict, init=False)

    # -- 计划构建 ----------------------------------------------------------

    def build_plan(
        self, opportunity: ArbitrageOpportunity, *, size_usd: float
    ) -> TradePlan:
        """把一个 :class:`ArbitrageOpportunity` 落成双腿 :class:`TradePlan`。

        套利的关键是**各腿买入等量合约 N**：在不同平台各买 N 份互补结果，无论最终
        哪个结果发生，组合都赔付 N×$1，从而锁定价差。``size_usd`` 是该计划的**总预算**，
        因此合约数 ``N = size_usd / 每对成本``，其中「每对成本」= 各腿目标价之和
        （买齐所有互补结果各一份的总价）。每条腿都用同一个 N，确保真正对冲、且总投入
        不超过 ``size_usd``。

        注意：此前的实现按「每腿 size_usd / 腿价」独立折算，会导致 (a) 实际部署约
        2×size_usd（与风控核准不符），(b) 两腿合约数不等、不构成对冲。本实现修正之。
        """
        now = self.clock()
        cost_per_pair = sum(
            leg.price for leg in opportunity.legs if leg.price > 0
        )
        # 每对合约的总成本必须为正才能折算数量；否则数量为 0（不可成交）。
        contracts = (size_usd / cost_per_pair) if cost_per_pair > 0 else 0.0
        legs: List[TradeLeg] = []
        for arb_leg in opportunity.legs:
            legs.append(
                TradeLeg(
                    platform=arb_leg.platform,
                    market_id=arb_leg.market_id,
                    outcome=arb_leg.outcome,
                    side=OrderSide.BUY,
                    target_price=arb_leg.price,
                    quantity=contracts,  # 各腿等量合约，确保对冲
                    available_liquidity_usd=arb_leg.available_liquidity_usd,
                )
            )
        return TradePlan(
            plan_id=f"plan-{next(self._plan_seq)}",
            group_id=opportunity.group_id,
            event_title=opportunity.event_title,
            legs=legs,
            expected_net_profit_margin=opportunity.net_profit_margin,
            size_usd=size_usd,
            status=TradePlanStatus.PENDING_CONFIRMATION,
            created_at=now,
            updated_at=now,
        )

    # -- 执行 --------------------------------------------------------------

    async def execute_plan(self, plan: TradePlan, *, dry_run: bool = False) -> TradePlan:
        """执行一个已确认的计划，保证双腿原子性。

        逐腿下单：全部成交 → ``COMPLETED``；任一腿失败/未完全成交 → 触发补救
        （撤销/平掉已成交腿），计划置 ``FAILED`` 并在 ``notes`` 记录残余敞口。

        Args:
            plan: 待执行计划。
            dry_run: 为 True 时对**真实**（会动真钱的）适配器只演练不下单。**模拟盘
                （``is_paper=True``）不受 dry_run 影响，始终照常模拟成交**——模拟盘
                本就不动真钱，dry-run 对它无意义，让它照常成交才能在仪表盘看到持仓/
                收益变化。仅当计划涉及任一真实适配器时，dry_run=True 才走演练分支
                （记录「将要下的单」、不触达任何适配器、计划置 ``COMPLETED`` 标注
                ``[DRY-RUN]``）。

        返回更新后的计划（其 legs 的 ``order_id`` 已填充，便于审计/对账）。
        """
        # dry-run 只对真实适配器生效；全部为模拟盘时照常模拟成交（即使 dry_run=True）。
        if dry_run and not self._all_paper(plan):
            return self._dry_run(plan)

        plan.status = TradePlanStatus.EXECUTING
        plan.updated_at = self.clock()

        filled_orders: List[Order] = []  # 已成交（或部分成交）的腿，用于补救
        failure_reason: Optional[str] = None

        # 先难后易（降低 leg risk）：按可成交流动性**升序**执行——先下流动性最差/最难
        # 成交的腿。若它失败，此时尚未动用其它腿，无需补救、损失最小；只有最难的腿成了，
        # 才去下较容易的腿。流动性未知（None）视为最薄、排最前（最保守）。
        execution_order = sorted(
            plan.legs,
            key=lambda leg: (
                leg.available_liquidity_usd
                if leg.available_liquidity_usd is not None
                else 0.0
            ),
        )

        for leg in execution_order:
            adapter = self.adapters.get(leg.platform)
            if adapter is None:
                failure_reason = f"无 {leg.platform} 的执行适配器"
                break
            try:
                order = await adapter.place_order(
                    market_id=leg.market_id,
                    outcome=leg.outcome,
                    side=leg.side,
                    limit_price=leg.target_price,
                    quantity=leg.quantity,
                )
            except ExecutionError as exc:
                failure_reason = f"{leg.platform} 下单失败：{exc}"
                break

            leg.order_id = order.order_id
            if order.status is OrderStatus.FILLED:
                filled_orders.append(order)
            else:
                # 全成或撤（IOC / fill-or-kill）：未完全成交即视为该腿未达成，并**立即撤掉
                # 未成交部分**——绝不把一个挂单（resting order）留在簿上，否则它可能在我们
                # 补救完之后才成交，凭空制造新的单边裸敞口。部分成交的已成交量仍需补救平仓。
                failure_reason = (
                    f"{leg.platform} 腿未完全成交（状态 {order.status.value}）"
                )
                await self._kill_remainder(adapter, order)
                if order.filled_quantity > 0:
                    filled_orders.append(order)
                break

        if failure_reason is None and len(filled_orders) == len(plan.legs):
            # 双腿（全部腿）均成交。
            plan.status = TradePlanStatus.COMPLETED
            plan.updated_at = self.clock()
            self._record_realized_pnl(plan, filled_orders)
            # 暴露已成交订单供调用方持久化/对账。
            self.last_orders[plan.plan_id] = list(filled_orders)
            return plan

        # 进入补救：绝不放任单边裸敞口。
        await self._remediate(plan, filled_orders, failure_reason or "未知失败")
        self.last_orders[plan.plan_id] = list(filled_orders)
        return plan

    # （已实现收益核算见下方 _record_realized_pnl）

    def _record_realized_pnl(self, plan: TradePlan, filled_orders: List[Order]) -> None:
        """用**实际成交价**核算这笔套利的已实现收益（审计「是否真实有收益」）。

        二元套利在不同平台各买入互补结果各 N 份：无论哪个结果发生，恰有一边赔付
        $1×（该对冲覆盖的合约数）。因此：

        - 对冲赔付 = 各腿实际成交量的**最小值**（只有两腿都覆盖的部分才真正对冲）× $1。
        - 实际成本 = Σ（每腿成交量 × 成交均价）。
        - 已实现收益 = 赔付 − 成本；收益率 = 收益 / 成本。

        注意：用成交均价而非目标价，故能反映滑点带来的真实盈亏。若因滑点/费用导致
        成本 ≥ 赔付，收益为负——这正是要抓的「检测为正、实际不赚」的情形。
        """
        if not filled_orders:
            return
        cost = sum(
            (o.avg_fill_price or o.limit_price) * o.filled_quantity for o in filled_orders
        )
        # 对冲覆盖的合约数 = 各腿成交量最小值（多腿套利需所有腿都覆盖才算对冲）。
        hedged_contracts = min((o.filled_quantity for o in filled_orders), default=0.0)
        payoff = hedged_contracts * 1.0
        plan.filled_cost_usd = round(cost, 6)
        plan.expected_payoff_usd = round(payoff, 6)
        plan.realized_profit_usd = round(payoff - cost, 6)
        plan.realized_profit_margin = round((payoff - cost) / cost, 6) if cost > 0 else 0.0

    def _all_paper(self, plan: TradePlan) -> bool:
        """计划涉及的所有腿是否都由模拟盘适配器执行（无真实适配器、无真钱风险）。

        缺适配器的腿视为「非全模拟盘」（保守），让 dry-run 走演练分支而非误执行。
        """
        for leg in plan.legs:
            adapter = self.adapters.get(leg.platform)
            if adapter is None or not getattr(adapter, "is_paper", False):
                return False
        return True

    def _dry_run(self, plan: TradePlan) -> TradePlan:
        """演练执行：只记录「将要下的单」，绝不触达适配器、绝不动真钱。

        生产前验证的安全开关：核对各腿的平台/结果/价格/数量是否符合预期，
        计划置 ``COMPLETED`` 并在 ``notes`` 以 ``[DRY-RUN]`` 标注，不产生任何真实订单。
        """
        now = self.clock()
        intents = [
            f"{leg.platform} 买 {leg.outcome} {leg.quantity:.2f}@{leg.target_price}"
            for leg in plan.legs
        ]
        plan.status = TradePlanStatus.COMPLETED
        plan.notes = "[DRY-RUN] 演练（未真正下单）：" + " ＋ ".join(intents)
        plan.updated_at = now
        return plan

    async def _kill_remainder(self, adapter: ExecutionAdapter, order: Order) -> None:
        """全成或撤：撤掉一笔未完全成交订单的挂单/未成交部分。

        IOC（immediate-or-cancel）语义的落地：当一条腿没有全额成交（部分成交或挂单未成），
        必须立刻撤掉它在簿上的剩余挂单，**绝不让它在我们补救完之后才成交**而凭空产生新的
        单边裸敞口。已成交的部分由 :meth:`_remediate` 反向平仓处理；这里只负责取消未成交
        的剩余。撤单失败（订单可能已是终态）只记日志、不致命。
        """
        if order.platform_order_id is None:
            return
        try:
            await adapter.cancel_order(order.platform_order_id)
        except ExecutionError:
            logger.warning(
                "计划腿订单 %s 撤单失败（可能已终态），继续补救", order.order_id
            )

    async def _remediate(
        self, plan: TradePlan, filled_orders: List[Order], reason: str
    ) -> None:
        """一腿失败时的补救：尝试平掉/撤销已成交的腿，记录残余敞口。

        对每个已成交的腿，尝试反向卖出等量以对冲；卖出失败则记录为**残余敞口**，
        需人工介入。无论补救成功与否，计划都标记 ``FAILED`` 并在 notes 留痕。
        """
        notes: List[str] = [f"执行失败：{reason}", "触发补救以避免单边裸敞口。"]
        residual: List[str] = []

        for order in filled_orders:
            adapter = self.adapters.get(order.platform)
            if adapter is None:
                residual.append(f"{order.platform}:{order.market_id} 无适配器，无法平仓")
                continue
            try:
                # 反向卖出已成交数量以平掉敞口。
                unwind = await adapter.place_order(
                    market_id=order.market_id,
                    outcome=order.outcome,
                    side=OrderSide.SELL,
                    limit_price=order.avg_fill_price or order.limit_price,
                    quantity=order.filled_quantity,
                )
                if unwind.status is OrderStatus.FILLED:
                    notes.append(
                        f"已平仓 {order.platform}:{order.market_id} "
                        f"{order.filled_quantity}@{unwind.avg_fill_price}"
                    )
                else:
                    residual.append(
                        f"{order.platform}:{order.market_id} 平仓未成交"
                        f"（状态 {unwind.status.value}），残余敞口 {order.filled_quantity}"
                    )
            except ExecutionError as exc:
                residual.append(
                    f"{order.platform}:{order.market_id} 平仓失败：{exc}，"
                    f"残余敞口 {order.filled_quantity}"
                )

        if residual:
            notes.append("⚠️ 残余敞口需人工介入：" + "；".join(residual))
            logger.error(
                "计划 %s 执行失败且存在残余敞口：%s", plan.plan_id, "；".join(residual)
            )
        else:
            logger.warning("计划 %s 执行失败，已完成补救（无残余敞口）。", plan.plan_id)

        plan.status = TradePlanStatus.FAILED
        plan.notes = " ".join(notes)
        plan.updated_at = self.clock()

    def has_residual_exposure(self, plan: TradePlan) -> bool:
        """计划是否存在残余敞口（供告警/健康检查判断）。"""
        return plan.notes is not None and "残余敞口" in plan.notes


__all__ = ["ExecutionEngine"]
