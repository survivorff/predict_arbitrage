"""半自动交易编排服务(Phase Three · 切片 H)。

把「检测到的套利机会」推进为「经风控、待人工确认、可执行的交易计划」。这是把
切片 G 的风控闸门真正串进流程的一环,核心是**半自动**:机会 → 风控评估 →
生成待确认 ``TradePlan`` → 人工确认 → (dry-run)执行。绝不自动下真单。

流程:
1. ``propose(opportunities)``:每周期对可交易机会跑风控;通过的生成一个
   ``PENDING_CONFIRMATION`` 计划(核准规模来自风控)并持久化;不通过的记录被拒原因。
   去重:同一 group 已有未终结(待确认/已确认/执行中)计划时不重复生成。
2. ``confirm(plan_id)``:人工确认 → 计划置 ``CONFIRMED`` → 立即按 ``dry_run`` 执行
   (dry-run 只演练不下单)。返回更新后的计划。
3. ``reject(plan_id)``:人工拒绝 → 计划置 ``REJECTED``。

安全:
- 默认 ``dry_run=True`` 且 ``require_confirmation=True``(来自风控配置),真实下单需
  显式关闭 dry-run 且接入真实执行适配器(切片 I)。
- 所有计划经 ``TradeStore`` 持久化,可查询、可对账、可审计。
- 风控决策(含每条检查明细)记入计划 ``notes``,可解释。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from scanner.execution_engine import ExecutionEngine
from scanner.models import ArbitrageOpportunity, TradePlan, TradePlanStatus
from scanner.risk import RiskDecision, RiskManager
from scanner.trade_store import InMemoryTradeStore, TradeStore

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# 未终结(仍占用「该 group 有进行中计划」语义)的状态——用于提议去重。
_OPEN_STATUSES = frozenset(
    {
        TradePlanStatus.PENDING_CONFIRMATION,
        TradePlanStatus.CONFIRMED,
        TradePlanStatus.EXECUTING,
    }
)

# 占用资金敞口的状态：待确认/已确认/执行中预留或已部署资金，已完成的持仓在结算前
# 仍占用敞口。被拒/失败不占用。用于敞口上限自核算。
_EXPOSURE_STATUSES = frozenset(
    {
        TradePlanStatus.PENDING_CONFIRMATION,
        TradePlanStatus.CONFIRMED,
        TradePlanStatus.EXECUTING,
        TradePlanStatus.COMPLETED,
    }
)


class TradingService:
    """半自动交易编排:机会→风控→待确认计划→确认→(dry-run)执行(Phase 3 · 切片 H)。

    Args:
        risk_manager: 下单前风控闸门。
        execution_engine: 双腿原子性执行引擎(构建/执行计划)。
        store: 交易计划/订单持久化;省略则用内存实现。
        clock: 注入时钟,使时间戳确定。
    """

    def __init__(
        self,
        *,
        risk_manager: RiskManager,
        execution_engine: ExecutionEngine,
        store: Optional[TradeStore] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.risk_manager = risk_manager
        self.execution_engine = execution_engine
        self.store = store if store is not None else InMemoryTradeStore()
        self._clock = clock
        # group_id -> 最近一次被拒的风控决策(供呈现,不持久化)。
        self._rejections: Dict[str, RiskDecision] = {}

    # -- 提议(每周期) ----------------------------------------------------- #

    def propose(
        self,
        opportunities: List[ArbitrageOpportunity],
        *,
        current_total_exposure_usd: float = 0.0,
        market_exposure_usd: Optional[Dict[str, float]] = None,
        available_balance_usd: Optional[float] = None,
        observed_prices: Optional[Dict] = None,
    ) -> List[TradePlan]:
        """对一批机会跑风控,为通过的生成待确认计划(去重)。

        Returns:
            本次**新生成**的待确认计划列表(已通过风控且此前无进行中计划的 group)。
        """
        market_exposure_usd = market_exposure_usd or {}
        # 自核算当前敞口（若调用方未显式提供则用存储里的进行中/已完成计划估算），
        # 使总敞口/单市场敞口上限真正生效——此前 propose 不传敞口，上限形同虚设。
        plans = self.store.list_plans()
        accounted_total, per_market = self._current_exposure(plans)
        if current_total_exposure_usd <= 0.0:
            current_total_exposure_usd = accounted_total
        # 已有进行中计划的 group,避免重复提议。
        open_groups = {p.group_id for p in plans if p.status in _OPEN_STATUSES}
        new_plans: List[TradePlan] = []
        for opp in opportunities:
            if opp.group_id in open_groups:
                continue
            # 该机会触及市场的现有敞口取最大值（最紧的市场约束生效）；调用方可覆盖。
            mkt_exp = market_exposure_usd.get(opp.group_id)
            if mkt_exp is None:
                mkt_exp = max(
                    (per_market.get((leg.platform, leg.market_id), 0.0) for leg in opp.legs),
                    default=0.0,
                )
            decision = self.risk_manager.evaluate(
                opp,
                current_total_exposure_usd=current_total_exposure_usd,
                market_exposure_usd=mkt_exp,
                available_balance_usd=available_balance_usd,
                observed_prices=observed_prices,
            )
            if not decision.approved:
                self._rejections[opp.group_id] = decision
                logger.info(
                    "机会 %s 未通过风控,不生成计划:%s",
                    opp.group_id,
                    "；".join(decision.rejected_reasons),
                )
                continue
            plan = self.execution_engine.build_plan(
                opp, size_usd=decision.approved_size_usd
            )
            plan.notes = self._format_decision_note(decision)
            self.store.upsert_plan(plan)
            new_plans.append(plan)
            open_groups.add(opp.group_id)
            # 新计划即时计入敞口，使同一周期内的后续机会受累计敞口约束。
            current_total_exposure_usd += decision.approved_size_usd
            for leg in plan.legs:
                k = (leg.platform, leg.market_id)
                per_market[k] = per_market.get(k, 0.0) + decision.approved_size_usd
            logger.info(
                "机会 %s 通过风控,生成待确认计划 %s(核准规模 $%.2f,dry_run=%s)",
                opp.group_id,
                plan.plan_id,
                decision.approved_size_usd,
                decision.dry_run,
            )
        return new_plans

    def _current_exposure(self, plans):
        """从进行中/已完成计划估算 (总敞口, 各市场敞口) —— 被拒/失败不计。"""
        total = 0.0
        per_market: Dict = {}
        for p in plans:
            if p.status not in _EXPOSURE_STATUSES:
                continue
            total += p.size_usd
            for leg in p.legs:
                k = (leg.platform, leg.market_id)
                per_market[k] = per_market.get(k, 0.0) + p.size_usd
        return total, per_market

    # -- 人工确认 / 拒绝 --------------------------------------------------- #

    async def confirm(self, plan_id: str) -> TradePlan:
        """人工确认一个待确认计划,随即按风控 dry_run 标志执行。

        Raises:
            KeyError: 计划不存在。
            ValueError: 计划不处于待确认状态。
        """
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise KeyError(f"计划不存在:{plan_id}")
        if plan.status is not TradePlanStatus.PENDING_CONFIRMATION:
            raise ValueError(
                f"计划 {plan_id} 不可确认(当前状态 {plan.status.value})"
            )
        plan.status = TradePlanStatus.CONFIRMED
        plan.updated_at = self._clock()
        self.store.upsert_plan(plan)

        # 执行(dry-run 由风控配置决定:演练只记录不下单)。
        executed = await self.execution_engine.execute_plan(
            plan, dry_run=self.risk_manager.limits.dry_run
        )
        self.store.upsert_plan(executed)
        # 执行产生的已成交订单落库,便于对账/审计（非 dry-run 时）。
        for order in self.execution_engine.last_orders.get(executed.plan_id, []):
            self.store.upsert_order(order)
        if executed.realized_profit_usd is not None:
            logger.info(
                "计划 %s 已实现收益 $%.4f（收益率 %.4f）— %s",
                executed.plan_id,
                executed.realized_profit_usd,
                executed.realized_profit_margin or 0.0,
                "真实有收益" if executed.is_genuinely_profitable else "⚠️ 实际不盈利",
            )
        logger.info(
            "计划 %s 已确认并执行,状态 %s%s",
            plan_id,
            executed.status.value,
            "(dry-run)" if self.risk_manager.limits.dry_run else "",
        )
        return executed

    def reject(self, plan_id: str) -> TradePlan:
        """人工拒绝一个待确认计划。

        Raises:
            KeyError: 计划不存在。
            ValueError: 计划不处于待确认状态。
        """
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise KeyError(f"计划不存在:{plan_id}")
        if plan.status is not TradePlanStatus.PENDING_CONFIRMATION:
            raise ValueError(
                f"计划 {plan_id} 不可拒绝(当前状态 {plan.status.value})"
            )
        plan.status = TradePlanStatus.REJECTED
        plan.updated_at = self._clock()
        self.store.upsert_plan(plan)
        logger.info("计划 %s 已被人工拒绝。", plan_id)
        return plan

    # -- 查询 -------------------------------------------------------------- #

    def list_plans(self, *, status: Optional[TradePlanStatus] = None) -> List[TradePlan]:
        """列出计划,可按状态过滤;按创建时间升序。"""
        plans = self.store.list_plans()
        if status is not None:
            plans = [p for p in plans if p.status is status]
        return sorted(plans, key=lambda p: p.created_at)

    def get_plan(self, plan_id: str) -> Optional[TradePlan]:
        return self.store.get_plan(plan_id)

    def list_orders(self):
        return self.store.list_orders()

    async def get_balances(self) -> Dict[str, object]:
        """聚合查询各平台执行适配器的可用余额（USD），供「账户余额」展示。

        逐平台调用 `ExecutionAdapter.get_balance()`；某平台查询失败不影响其它平台
        （记 error）。当前为模拟盘余额，接入真实执行适配器（切片 I）后自动变为真实余额。
        """
        result: Dict[str, object] = {}
        for name, adapter in self.execution_engine.adapters.items():
            try:
                result[name] = {"balance_usd": await adapter.get_balance()}
            except Exception as exc:  # noqa: BLE001 - 单平台失败隔离
                result[name] = {"error": str(exc)}
        return result

    async def get_positions(self) -> List[Dict[str, object]]:
        """聚合查询各平台执行适配器的当前持仓，供「持仓」展示。

        逐平台调用 `ExecutionAdapter.get_positions()`；某平台失败不影响其它平台。
        """
        out: List[Dict[str, object]] = []
        for name, adapter in self.execution_engine.adapters.items():
            try:
                positions = await adapter.get_positions()
            except Exception:  # noqa: BLE001 - 单平台失败隔离
                continue
            for p in positions:
                out.append({
                    "platform": p.platform,
                    "market_id": p.market_id,
                    "outcome": p.outcome,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                })
        return out

    def exposure_snapshot(self) -> Dict[str, object]:
        """当前敞口快照（总敞口 + 各市场敞口），供风控可视化与审计。"""
        total, per_market = self._current_exposure(self.store.list_plans())
        return {
            "total_exposure_usd": round(total, 2),
            "per_market": [
                {"platform": k[0], "market_id": k[1], "exposure_usd": round(v, 2)}
                for k, v in sorted(per_market.items())
            ],
        }

    # -- 内部 -------------------------------------------------------------- #

    def _format_decision_note(self, decision: RiskDecision) -> str:
        flags = []
        if decision.dry_run:
            flags.append("DRY-RUN")
        if decision.require_confirmation:
            flags.append("需人工确认")
        prefix = f"[风控通过 · {' · '.join(flags)}] " if flags else "[风控通过] "
        passed = ", ".join(c.name for c in decision.checks if c.passed)
        return f"{prefix}核准规模 ${decision.approved_size_usd:.2f};通过检查:{passed}"

    def pnl_summary(self) -> Dict[str, object]:
        """已执行计划的真实盈亏汇总（审计「每笔是否真实有收益」）。

        遍历已完成且有真实成交核算的计划，统计累计已实现收益、盈利/亏损笔数，
        并逐笔列出（事件、检测利润率、实际成交成本、赔付、已实现收益与收益率、
        是否真实盈利）。dry-run（演练）计划不计入（无真实成交）。
        """
        completed = [
            p for p in self.store.list_plans()
            if p.realized_profit_usd is not None
        ]
        total = sum(p.realized_profit_usd or 0.0 for p in completed)
        profitable = sum(1 for p in completed if (p.realized_profit_usd or 0.0) > 0)
        losing = sum(1 for p in completed if (p.realized_profit_usd or 0.0) <= 0)
        trades = [
            {
                "plan_id": p.plan_id,
                "event_title": p.event_title,
                "expected_margin": p.expected_net_profit_margin,
                "filled_cost_usd": p.filled_cost_usd,
                "expected_payoff_usd": p.expected_payoff_usd,
                "realized_profit_usd": p.realized_profit_usd,
                "realized_profit_margin": p.realized_profit_margin,
                "genuinely_profitable": p.is_genuinely_profitable,
            }
            for p in sorted(completed, key=lambda x: x.created_at)
        ]
        return {
            "executed_trades": len(completed),
            "profitable_trades": profitable,
            "losing_trades": losing,
            "total_realized_profit_usd": round(total, 6),
            "trades": trades,
        }


__all__ = ["TradingService"]
