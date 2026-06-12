"""半自动交易编排服务测试（Phase 3 · 切片 H）。

验证 :class:`scanner.trading.TradingService` 的完整链路:
- propose:风控通过 → 生成待确认计划；不通过 → 不生成；同 group 去重。
- confirm:待确认 → CONFIRMED → 按 dry-run 执行 → COMPLETED（dry-run 不动真钱）。
- reject:待确认 → REJECTED。
- 非待确认状态 confirm/reject 抛 ValueError；不存在抛 KeyError。
- list_plans 状态过滤与排序。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scanner.execution import PaperExecutionAdapter
from scanner.execution_engine import ExecutionEngine
from scanner.models import ArbLeg, ArbitrageOpportunity, TradePlanStatus
from scanner.risk import RiskLimits, RiskManager
from scanner.trade_store import InMemoryTradeStore
from scanner.trading import TradingService

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _opp(group_id: str = "g1", *, margin: float = 0.10, size: float = 100.0,
         data_age: float = 5.0, distinct_markets: bool = False) -> ArbitrageOpportunity:
    # distinct_markets=True 时每个 group 用各自的市场，避免共享市场的单市场敞口
    # 上限干扰总敞口测试。
    suffix = group_id if distinct_markets else ""
    return ArbitrageOpportunity(
        group_id=group_id,
        event_title=f"事件 {group_id}",
        legs=[
            ArbLeg(platform="polymarket", market_id=f"p1{suffix}", outcome="YES", price=0.40),
            ArbLeg(platform="predictfun", market_id=f"1471{suffix}", outcome="NO", price=0.45),
        ],
        net_profit_margin=margin,
        recommended_size_usd=size,
        detected_at=BASE_TIME,
        data_age_seconds=data_age,
    )


def _service(*, limits: RiskLimits = None, clock: FakeClock = None) -> TradingService:
    clock = clock or FakeClock()
    adapters = {
        "polymarket": PaperExecutionAdapter(name="polymarket", starting_balance_usd=10_000.0, clock=clock),
        "predictfun": PaperExecutionAdapter(name="predictfun", starting_balance_usd=10_000.0, clock=clock),
    }
    return TradingService(
        risk_manager=RiskManager(limits or RiskLimits()),
        execution_engine=ExecutionEngine(adapters=adapters, clock=clock),
        store=InMemoryTradeStore(),
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# propose
# --------------------------------------------------------------------------- #

def test_propose_creates_plan_for_approved_opportunity():
    svc = _service()
    plans = svc.propose([_opp(margin=0.10)])
    assert len(plans) == 1
    plan = plans[0]
    assert plan.status is TradePlanStatus.PENDING_CONFIRMATION
    assert plan.size_usd == 100.0  # 风控核准规模（单笔上限）
    assert plan.notes is not None and "风控通过" in plan.notes


def test_propose_skips_rejected_opportunity():
    # 净利润率低于门槛 → 风控拦截 → 不生成计划。
    svc = _service(limits=RiskLimits(min_net_profit_margin=0.5))
    plans = svc.propose([_opp(margin=0.10)])
    assert plans == []
    assert svc.list_plans() == []


def test_propose_dedupes_same_group():
    svc = _service()
    svc.propose([_opp("g1")])
    # 同一 group 再次提议不重复生成（已有待确认计划）。
    plans2 = svc.propose([_opp("g1")])
    assert plans2 == []
    assert len(svc.list_plans()) == 1


def test_propose_multiple_groups():
    svc = _service()
    plans = svc.propose([_opp("g1"), _opp("g2")])
    assert len(plans) == 2
    assert {p.group_id for p in plans} == {"g1", "g2"}


# --------------------------------------------------------------------------- #
# confirm
# --------------------------------------------------------------------------- #

async def test_confirm_paper_simulates_under_dry_run():
    # 修复：模拟盘在 dry_run=True 下仍模拟成交、建仓、核算收益（持仓会变）。
    clock = FakeClock()
    svc = _service(limits=RiskLimits(dry_run=True, min_net_profit_margin=0.0), clock=clock)
    [plan] = svc.propose([_opp(size=85.0)])

    confirmed = await svc.confirm(plan.plan_id)
    assert confirmed.status is TradePlanStatus.COMPLETED
    # 模拟盘照常成交：腿关联订单、有已实现收益、持仓更新。
    assert all(leg.order_id is not None for leg in confirmed.legs)
    assert confirmed.realized_profit_usd is not None
    poly_pos = await svc.execution_engine.adapters["polymarket"].get_positions()
    assert len(poly_pos) == 1
    assert svc.get_plan(plan.plan_id).status is TradePlanStatus.COMPLETED


async def test_confirm_real_execution_when_dry_run_off():
    # dry_run=False → 真正经模拟盘下单 → COMPLETED（仍是 paper，不动真钱但会建仓）。
    clock = FakeClock()
    svc = _service(limits=RiskLimits(dry_run=False), clock=clock)
    [plan] = svc.propose([_opp()])
    confirmed = await svc.confirm(plan.plan_id)
    assert confirmed.status is TradePlanStatus.COMPLETED
    # 非 dry-run 时腿应关联真实（模拟盘）订单。
    assert all(leg.order_id is not None for leg in confirmed.legs)


async def test_confirm_unknown_plan_raises_keyerror():
    svc = _service()
    with pytest.raises(KeyError):
        await svc.confirm("nope")


async def test_confirm_non_pending_raises_valueerror():
    svc = _service()
    [plan] = svc.propose([_opp()])
    await svc.confirm(plan.plan_id)  # 变为 COMPLETED
    with pytest.raises(ValueError):
        await svc.confirm(plan.plan_id)  # 再次确认应失败


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #

def test_reject_marks_plan_rejected():
    svc = _service()
    [plan] = svc.propose([_opp()])
    rejected = svc.reject(plan.plan_id)
    assert rejected.status is TradePlanStatus.REJECTED


def test_reject_unknown_raises_keyerror():
    svc = _service()
    with pytest.raises(KeyError):
        svc.reject("nope")


def test_reject_non_pending_raises_valueerror():
    svc = _service()
    [plan] = svc.propose([_opp()])
    svc.reject(plan.plan_id)
    with pytest.raises(ValueError):
        svc.reject(plan.plan_id)


def test_rejected_group_can_be_reproposed():
    # 拒绝后该 group 不再占用「进行中」，可重新提议。
    svc = _service()
    [plan] = svc.propose([_opp("g1")])
    svc.reject(plan.plan_id)
    plans2 = svc.propose([_opp("g1")])
    assert len(plans2) == 1


# --------------------------------------------------------------------------- #
# list_plans 过滤
# --------------------------------------------------------------------------- #

def test_list_plans_status_filter():
    svc = _service()
    svc.propose([_opp("g1"), _opp("g2")])
    plans = svc.list_plans()
    svc.reject(plans[0].plan_id)
    pending = svc.list_plans(status=TradePlanStatus.PENDING_CONFIRMATION)
    rejected = svc.list_plans(status=TradePlanStatus.REJECTED)
    assert len(pending) == 1
    assert len(rejected) == 1


# --------------------------------------------------------------------------- #
# 敞口上限自核算（修复 #5/#6）：propose 不传敞口时也应让总敞口上限生效
# --------------------------------------------------------------------------- #

def test_total_exposure_limit_binds_across_plans():
    # 总敞口上限 250，单笔上限 100：前两个机会各占 100（累计 200），
    # 第三个本应再占 100 但总剩余只剩 50 → 核准规模被压到 50。
    svc = _service(limits=RiskLimits(max_trade_size_usd=100.0, max_total_exposure_usd=250.0))
    svc.propose([_opp("g1", size=100.0, distinct_markets=True)])
    svc.propose([_opp("g2", size=100.0, distinct_markets=True)])
    new = svc.propose([_opp("g3", size=100.0, distinct_markets=True)])
    assert len(new) == 1
    # 第三个计划受总敞口余量（250-200=50）约束。
    assert new[0].size_usd == pytest.approx(50.0)


def test_total_exposure_limit_rejects_when_exhausted():
    # 总敞口上限 200：两个 100 占满后，第三个机会无敞口余量 → 不生成计划。
    svc = _service(limits=RiskLimits(max_trade_size_usd=100.0, max_total_exposure_usd=200.0))
    svc.propose([_opp("g1", size=100.0, distinct_markets=True)])
    svc.propose([_opp("g2", size=100.0, distinct_markets=True)])
    new = svc.propose([_opp("g3", size=100.0, distinct_markets=True)])
    assert new == []


def test_within_cycle_exposure_accumulates():
    # 同一周期内多个机会，累计敞口即时生效：总上限 150 只够 1.5 个 100。
    svc = _service(limits=RiskLimits(max_trade_size_usd=100.0, max_total_exposure_usd=150.0))
    new = svc.propose([
        _opp("g1", size=100.0, distinct_markets=True),
        _opp("g2", size=100.0, distinct_markets=True),
    ])
    sizes = sorted(p.size_usd for p in new)
    # 第一个占 100，第二个只剩 50。
    assert sizes == [pytest.approx(50.0), pytest.approx(100.0)]


def test_per_market_exposure_limit_binds():
    # 同一市场重复机会：单市场敞口上限 120，单笔 100。第一个占 100，
    # 第二个（同市场）市场剩余 20 → 压到 20。
    svc = _service(limits=RiskLimits(
        max_trade_size_usd=100.0, max_market_exposure_usd=120.0, max_total_exposure_usd=10_000.0,
    ))
    svc.propose([_opp("g1", size=100.0)])  # 共享市场 p1/1471
    new = svc.propose([_opp("g2", size=100.0)])  # 同市场
    assert len(new) == 1
    assert new[0].size_usd == pytest.approx(20.0)


# --------------------------------------------------------------------------- #
# 真实收益核算与汇总（任务 2）
# --------------------------------------------------------------------------- #

async def test_pnl_summary_after_real_execution():
    # 非 dry-run 执行后，计划带已实现收益，pnl_summary 正确汇总。
    svc = _service(limits=RiskLimits(dry_run=False, min_net_profit_margin=0.0))
    [plan] = svc.propose([_opp("g1", size=85.0)])  # 每对成本 0.85
    await svc.confirm(plan.plan_id)
    summary = svc.pnl_summary()
    assert summary["executed_trades"] == 1
    assert summary["profitable_trades"] == 1
    assert summary["total_realized_profit_usd"] == pytest.approx(15.0)
    assert summary["trades"][0]["genuinely_profitable"] is True


async def test_confirm_persists_filled_orders():
    # 非 dry-run 确认后，已成交订单落库可对账。
    svc = _service(limits=RiskLimits(dry_run=False, min_net_profit_margin=0.0))
    [plan] = svc.propose([_opp("g1", size=85.0)])
    await svc.confirm(plan.plan_id)
    assert len(svc.list_orders()) == 2


async def test_real_adapter_dry_run_pnl_summary_empty():
    # 真实（非模拟盘）适配器在 dry-run 下只演练、不成交 → pnl_summary 不计入。
    class RealLikeAdapter:
        is_paper = False
        def __init__(self, name): self.name = name
        async def place_order(self, **kw): raise AssertionError("不应触达")
        async def get_balance(self): return 1000.0
        async def get_positions(self): return []
    eng = ExecutionEngine(adapters={
        "polymarket": RealLikeAdapter("polymarket"),
        "predictfun": RealLikeAdapter("predictfun"),
    })
    svc = TradingService(
        risk_manager=RiskManager(RiskLimits(dry_run=True, min_net_profit_margin=0.0)),
        execution_engine=eng, store=InMemoryTradeStore(),
    )
    [plan] = svc.propose([_opp("g1", size=85.0)])
    await svc.confirm(plan.plan_id)
    summary = svc.pnl_summary()
    assert summary["executed_trades"] == 0  # 真实适配器 dry-run 不成交


# --------------------------------------------------------------------------- #
# 紧急停机开关（kill switch）
# --------------------------------------------------------------------------- #

def test_halt_blocks_new_proposals():
    """停机后 propose 不再生成任何新计划。"""
    svc = _service()
    svc.halt(reason="test")
    assert svc.is_halted is True
    assert svc.propose([_opp(margin=0.10)]) == []
    # 恢复后又能生成。
    svc.resume()
    assert svc.is_halted is False
    assert len(svc.propose([_opp(margin=0.10)])) == 1


async def test_halt_blocks_confirm_execution():
    """停机后确认计划被拒绝（PermissionError），不触发任何执行。"""
    svc = _service()
    [plan] = svc.propose([_opp()])
    svc.halt(reason="emergency")
    import pytest
    with pytest.raises(PermissionError):
        await svc.confirm(plan.plan_id)
    # 计划仍停留在待确认（未被执行）。
    assert svc.get_plan(plan.plan_id).status is TradePlanStatus.PENDING_CONFIRMATION


def test_halt_state_and_idempotency():
    """halt/resume 幂等，halt_state 反映状态。"""
    svc = _service()
    assert svc.halt_state()["halted"] is False
    svc.halt(reason="r1"); svc.halt(reason="r2")  # 幂等：第二次不覆盖原因
    st = svc.halt_state()
    assert st["halted"] is True and st["reason"] == "r1" and st["halted_at"] is not None
    svc.resume(); svc.resume()
    assert svc.halt_state()["halted"] is False
