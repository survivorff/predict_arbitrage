"""套利执行引擎（ExecutionEngine）测试：核心关注双腿原子性（Phase Three · 切片 F）。

套利的头号风险是单边裸敞口——一腿成交、另一腿失败时若不补救就可能巨亏。本测试用
确定性时钟与可注入成交/拒单行为的执行适配器，严格覆盖以下路径：

- build_plan 把机会落成双腿计划（数量折算、状态、plan_id）。
- 双腿全成 → COMPLETED，两腿 order_id 写回，无残余敞口，两平台各有持仓。
- 第二腿拒单 → 补救平掉第一腿 → FAILED，无残余敞口（平仓成交）。
- 第一腿就失败 → 无已成交腿可平 → FAILED，无残余敞口。
- 平仓也失败 → 残余敞口（裸敞口）→ FAILED 且 has_residual_exposure=True。
- 缺适配器的腿 → 失败并触发补救。
- 部分成交的腿 → 视为未达成 → 触发补救。
- 注入时钟 → plan.updated_at 跟随注入时钟推进。

asyncio_mode=auto，故 async 测试无需显式标记。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import pytest

from scanner.execution import (
    ExecutionError,
    PaperExecutionAdapter,
)
from scanner.execution_engine import ExecutionEngine
from scanner.models import (
    ArbitrageOpportunity,
    ArbLeg,
    Order,
    OrderSide,
    OrderStatus,
    TradePlanStatus,
)

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定、可前进的 UTC 时钟，供注入使用（参考 tests/test_execution_paper.py）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_opportunity(
    *,
    poly_price: float = 0.40,
    kalshi_price: float = 0.45,
    poly_market: str = "poly-m1",
    kalshi_market: str = "kalshi-m1",
) -> ArbitrageOpportunity:
    """构造一个含两腿（polymarket YES、kalshi NO）的套利机会。"""
    return ArbitrageOpportunity(
        group_id="grp-1",
        event_title="2024 大选结果",
        legs=[
            ArbLeg(
                platform="polymarket",
                market_id=poly_market,
                outcome="YES",
                price=poly_price,
            ),
            ArbLeg(
                platform="kalshi",
                market_id=kalshi_market,
                outcome="NO",
                price=kalshi_price,
            ),
        ],
        net_profit_margin=0.15,
        recommended_size_usd=100.0,
        detected_at=BASE_TIME,
        data_age_seconds=1.0,
    )


# --------------------------------------------------------------------------- #
# 自定义假执行适配器：买入成功、卖出被拒。
# --------------------------------------------------------------------------- #
class BuyFillsSellRejectsAdapter:
    """买入返回 FILLED 订单、卖出抛 ExecutionError 的最小假执行适配器。

    用于构造「第一腿买入成交、补救平仓（卖出）失败」从而产生残余敞口的场景——
    PaperExecutionAdapter 的 reject_markets 对买卖双向都拒，无法单独让卖出失败。
    """

    def __init__(self, name: str, clock: FakeClock) -> None:
        self.name = name
        self._clock = clock
        self._seq = 0
        self.sell_attempts = 0  # 记录补救平仓被调用次数，便于断言

    async def place_order(
        self,
        *,
        market_id: str,
        outcome: str,
        side: OrderSide,
        limit_price: float,
        quantity: float,
    ) -> Order:
        if side is OrderSide.SELL:
            self.sell_attempts += 1
            raise ExecutionError(
                f"{self.name} 平仓被拒（模拟）", platform=self.name
            )
        self._seq += 1
        now = self._clock()
        oid = f"{self.name}-{self._seq}"
        return Order(
            order_id=oid,
            platform=self.name,
            market_id=market_id,
            outcome=outcome,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            status=OrderStatus.FILLED,
            platform_order_id=oid,
            filled_quantity=quantity,
            avg_fill_price=limit_price,
            created_at=now,
            updated_at=now,
        )

    async def cancel_order(self, platform_order_id: str) -> Order:  # pragma: no cover
        raise ExecutionError("not supported", platform=self.name)

    async def get_order(self, platform_order_id: str) -> Order:  # pragma: no cover
        raise ExecutionError("not supported", platform=self.name)

    async def get_balance(self) -> float:  # pragma: no cover
        return 0.0

    async def get_positions(self) -> List:  # pragma: no cover
        return []


def half_fill(limit_price: float, quantity: float) -> Tuple[float, float]:
    """部分成交策略：只成交一半。"""
    return quantity / 2.0, limit_price


class PartialBuyFullSellAdapter:
    """买入只部分成交、卖出（补救平仓）全额成交的最小假执行适配器。

    用于隔离「部分成交腿被视为未达成 → 触发补救」这一概念：买入部分成交触发对第一腿
    的补救，同时该部分成交腿自身的反向平仓能全额成交 → 最终无残余敞口。
    """

    def __init__(self, name: str, clock: FakeClock) -> None:
        self.name = name
        self._clock = clock
        self._seq = 0

    async def place_order(
        self,
        *,
        market_id: str,
        outcome: str,
        side: OrderSide,
        limit_price: float,
        quantity: float,
    ) -> Order:
        self._seq += 1
        now = self._clock()
        oid = f"{self.name}-{self._seq}"
        if side is OrderSide.SELL:
            # 平仓全额成交。
            filled = quantity
            status = OrderStatus.FILLED
        else:
            # 买入只部分成交。
            filled = quantity / 2.0
            status = OrderStatus.PARTIALLY_FILLED
        return Order(
            order_id=oid,
            platform=self.name,
            market_id=market_id,
            outcome=outcome,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            status=status,
            platform_order_id=oid,
            filled_quantity=filled,
            avg_fill_price=limit_price,
            created_at=now,
            updated_at=now,
        )

    async def cancel_order(self, platform_order_id: str) -> Order:  # pragma: no cover
        raise ExecutionError("not supported", platform=self.name)

    async def get_order(self, platform_order_id: str) -> Order:  # pragma: no cover
        raise ExecutionError("not supported", platform=self.name)

    async def get_balance(self) -> float:  # pragma: no cover
        return 0.0

    async def get_positions(self) -> List:  # pragma: no cover
        return []


def no_fill(limit_price: float, quantity: float) -> Tuple[float, float]:
    """挂单不成交策略。"""
    return 0.0, limit_price


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #
def test_build_plan_maps_opportunity_to_two_leg_plan() -> None:
    """build_plan：两腿机会落成两条 TradeLeg，数量按 size_usd/腿价折算，状态待确认。"""
    clock = FakeClock()
    engine = ExecutionEngine(adapters={}, clock=clock)
    opp = make_opportunity(poly_price=0.40, kalshi_price=0.45)

    plan = engine.build_plan(opp, size_usd=100.0)

    assert plan.plan_id  # 非空
    assert plan.status is TradePlanStatus.PENDING_CONFIRMATION
    assert plan.size_usd == 100.0
    assert plan.group_id == "grp-1"
    assert plan.event_title == "2024 大选结果"
    assert len(plan.legs) == 2

    poly_leg, kalshi_leg = plan.legs
    assert poly_leg.platform == "polymarket"
    assert poly_leg.outcome == "YES"
    assert poly_leg.side is OrderSide.BUY
    assert poly_leg.target_price == pytest.approx(0.40)
    # 套利买等量合约 N = size_usd / 每对成本。每对成本 = 0.40 + 0.45 = 0.85。
    # N = 100 / 0.85 ≈ 117.65。两腿数量相等（真正对冲）。
    expected_contracts = 100.0 / (0.40 + 0.45)
    assert poly_leg.quantity == pytest.approx(expected_contracts)

    assert kalshi_leg.platform == "kalshi"
    assert kalshi_leg.outcome == "NO"
    assert kalshi_leg.target_price == pytest.approx(0.45)
    # 两腿合约数相等，确保无论哪个结果发生都赔付 N×$1。
    assert kalshi_leg.quantity == pytest.approx(expected_contracts)
    assert poly_leg.quantity == pytest.approx(kalshi_leg.quantity)

    # 时间戳跟随注入时钟。
    assert plan.created_at == BASE_TIME
    assert plan.updated_at == BASE_TIME


def test_build_plan_ids_are_unique_and_incrementing() -> None:
    """连续 build_plan 的 plan_id 递增唯一。"""
    engine = ExecutionEngine(adapters={}, clock=FakeClock())
    p1 = engine.build_plan(make_opportunity(), size_usd=50.0)
    p2 = engine.build_plan(make_opportunity(), size_usd=50.0)
    assert p1.plan_id != p2.plan_id


# --------------------------------------------------------------------------- #
# 双腿全成
# --------------------------------------------------------------------------- #
async def test_execute_plan_both_legs_fill_completes() -> None:
    """双腿全成：status=COMPLETED，两腿 order_id 写回，无残余敞口，两平台各有持仓。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.COMPLETED
    assert all(leg.order_id is not None for leg in result.legs)
    assert engine.has_residual_exposure(result) is False

    # 两平台各有一笔持仓。
    assert len(await poly.get_positions()) == 1
    assert len(await kalshi.get_positions()) == 1


# --------------------------------------------------------------------------- #
# 第二腿拒单 → 补救平掉第一腿
# --------------------------------------------------------------------------- #
async def test_second_leg_rejected_remediates_first_leg() -> None:
    """第二腿（kalshi）拒单：补救平掉第一腿 polymarket → FAILED，无残余敞口。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(
        name="kalshi",
        starting_balance_usd=1000.0,
        reject_markets=frozenset({"kalshi-m1"}),
        clock=clock,
    )
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(kalshi_market="kalshi-m1"), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert engine.has_residual_exposure(result) is False
    # notes 记录了已平掉 polymarket 腿。
    assert result.notes is not None
    assert "已平仓 polymarket" in result.notes
    # 第一腿确有订单号写回（已成交），第二腿未成交无平仓残余。
    assert result.legs[0].order_id is not None


# --------------------------------------------------------------------------- #
# 第一腿就失败 → 无可平
# --------------------------------------------------------------------------- #
async def test_first_leg_fails_no_filled_to_unwind() -> None:
    """第一腿余额不足直接失败：无已成交腿可平 → FAILED，无残余敞口，notes 记录原因。"""
    clock = FakeClock()
    # polymarket 余额极低，第一腿（100/0.40=250 张 @0.40 → 成本 100）超出余额 → 拒。
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert engine.has_residual_exposure(result) is False
    assert result.notes is not None
    assert "下单失败" in result.notes
    # 第二腿从未尝试（第一腿失败即停），其 order_id 仍为 None。
    assert result.legs[1].order_id is None
    # kalshi 没有任何持仓（未被触达）。
    assert await kalshi.get_positions() == []


async def test_first_leg_rejected_no_residual() -> None:
    """第一腿拒单：无已成交腿 → FAILED，无残余敞口。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(
        name="polymarket",
        starting_balance_usd=1000.0,
        reject_markets=frozenset({"poly-m1"}),
        clock=clock,
    )
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(poly_market="poly-m1"), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert engine.has_residual_exposure(result) is False


# --------------------------------------------------------------------------- #
# 平仓也失败 → 残余敞口（裸敞口）
# --------------------------------------------------------------------------- #
async def test_unwind_failure_produces_residual_exposure() -> None:
    """第一腿买入成交、第二腿失败、补救平仓抛错 → FAILED 且 has_residual_exposure=True。

    这是资金安全最关键的路径：补救失败留下单边裸敞口，必须被明确标记并记录。
    """
    clock = FakeClock()
    # polymarket：买入成交、卖出（补救平仓）抛 ExecutionError。
    poly = BuyFillsSellRejectsAdapter(name="polymarket", clock=clock)
    # kalshi：第二腿拒单，触发对第一腿的补救。
    kalshi = PaperExecutionAdapter(
        name="kalshi",
        starting_balance_usd=1000.0,
        reject_markets=frozenset({"kalshi-m1"}),
        clock=clock,
    )
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(kalshi_market="kalshi-m1"), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert engine.has_residual_exposure(result) is True
    assert result.notes is not None
    assert "残余敞口" in result.notes
    # 补救确实尝试过平仓（卖出被调用一次）。
    assert poly.sell_attempts == 1


# --------------------------------------------------------------------------- #
# 缺适配器
# --------------------------------------------------------------------------- #
async def test_missing_adapter_for_leg_triggers_remediation() -> None:
    """第二腿平台无适配器：第一腿成交后进入补救平仓 → FAILED，notes 记录缺适配器。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    # 故意不提供 kalshi 适配器。
    engine = ExecutionEngine(adapters={"polymarket": poly}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert result.notes is not None
    assert "kalshi" in result.notes
    # 第一腿成交并被平掉，无残余敞口。
    assert engine.has_residual_exposure(result) is False
    assert "已平仓 polymarket" in result.notes


async def test_missing_adapter_for_first_leg_no_filled() -> None:
    """第一腿平台无适配器：无任何成交 → FAILED，无残余敞口。"""
    clock = FakeClock()
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    # 故意不提供 polymarket 适配器（第一腿）。
    engine = ExecutionEngine(adapters={"kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert engine.has_residual_exposure(result) is False
    assert result.legs[0].order_id is None


# --------------------------------------------------------------------------- #
# 部分成交的腿
# --------------------------------------------------------------------------- #
async def test_partial_fill_second_leg_triggers_remediation() -> None:
    """第二腿部分成交：视为未达成 → 触发补救（第一腿全成需平仓）→ FAILED。

    第二腿部分成交（filled_quantity>0）也会被补救尝试平仓；这里让第二腿适配器的
    卖出能全额成交，从而最终无残余敞口，专注验证「部分成交=未达成→触发补救」。
    """
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    # kalshi 买入只部分成交、卖出（平仓）全额成交。
    kalshi = PartialBuyFullSellAdapter(name="kalshi", clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    # 第一腿全成被平掉；第二腿部分成交也被全额平掉 → 无残余敞口。
    assert engine.has_residual_exposure(result) is False
    assert result.notes is not None
    assert "未完全成交" in result.notes
    # 两腿都拿到了 order_id（第二腿部分成交也有订单）。
    assert result.legs[0].order_id is not None
    assert result.legs[1].order_id is not None


async def test_partial_fill_leg_unwound_partially_leaves_residual() -> None:
    """第二腿部分成交、且其平仓也只能部分成交 → 残余敞口（资金安全关键路径）。

    用同一个 half_fill 策略：买入半成、补救卖出也半成，部分成交腿无法完全对冲 →
    必须被标记为残余敞口。验证引擎不会把「平仓未完全成交」误判为已对冲。
    """
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(
        name="kalshi",
        starting_balance_usd=1000.0,
        fill_policy=half_fill,
        clock=clock,
    )
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    # kalshi 腿部分成交、平仓也只半成 → 残余敞口。
    assert engine.has_residual_exposure(result) is True
    assert result.notes is not None
    assert "残余敞口" in result.notes


# --------------------------------------------------------------------------- #
# 时钟注入
# --------------------------------------------------------------------------- #
async def test_injected_clock_drives_updated_at_completed() -> None:
    """注入时钟：COMPLETED 计划的 updated_at 等于推进后的注入时钟。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(), size_usd=100.0)
    assert plan.updated_at == BASE_TIME

    # 推进时钟，执行后 updated_at 应等于新的时钟值。
    clock.advance(30.0)
    result = await engine.execute_plan(plan)
    assert result.status is TradePlanStatus.COMPLETED
    assert result.updated_at == BASE_TIME + timedelta(seconds=30.0)


async def test_injected_clock_drives_updated_at_failed() -> None:
    """注入时钟：补救后 FAILED 计划的 updated_at 也跟随注入时钟。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(
        name="kalshi",
        starting_balance_usd=1000.0,
        reject_markets=frozenset({"kalshi-m1"}),
        clock=clock,
    )
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    plan = engine.build_plan(make_opportunity(kalshi_market="kalshi-m1"), size_usd=100.0)
    clock.advance(45.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    assert result.updated_at == BASE_TIME + timedelta(seconds=45.0)


# --------------------------------------------------------------------------- #
# dry-run 模式（Phase 3 · 切片 G）：演练不真正下单。
# --------------------------------------------------------------------------- #

async def test_dry_run_with_real_adapter_does_not_touch() -> None:
    """dry-run + 真实（非模拟盘）适配器：绝不触达适配器，演练记录 notes。"""
    clock = FakeClock()

    class RealLikeAdapter:
        is_paper = False  # 标识为真实适配器
        def __init__(self, name):
            self.name = name
            self.place_calls = 0
        async def place_order(self, **kw):
            self.place_calls += 1
            raise AssertionError("dry-run 不应触达真实适配器")
        async def get_balance(self): return 1000.0
        async def get_positions(self): return []

    poly = RealLikeAdapter("polymarket")
    kalshi = RealLikeAdapter("kalshi")
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)
    plan = engine.build_plan(make_opportunity(), size_usd=100.0)

    result = await engine.execute_plan(plan, dry_run=True)

    assert result.status is TradePlanStatus.COMPLETED
    assert result.notes is not None and "[DRY-RUN]" in result.notes
    assert poly.place_calls == 0 and kalshi.place_calls == 0  # 未触达
    assert all(leg.order_id is None for leg in result.legs)


async def test_paper_simulates_even_under_dry_run() -> None:
    """关键修复：模拟盘(paper)在 dry_run=True 下仍照常模拟成交、更新持仓。

    模拟盘不动真钱，dry-run 对它无意义；让它照常成交，用户在默认 dry-run 配置下
    也能在仪表盘看到持仓/收益变化（修复「确认后持仓不变」的问题）。
    """
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=1000.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=1000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)
    plan = engine.build_plan(make_opportunity(poly_price=0.40, kalshi_price=0.45), size_usd=85.0)

    result = await engine.execute_plan(plan, dry_run=True)  # 默认 dry-run，但全是模拟盘

    assert result.status is TradePlanStatus.COMPLETED
    # 模拟盘照常成交 → 持仓更新、余额扣减、已实现收益核算。
    poly_pos = await poly.get_positions()
    kalshi_pos = await kalshi.get_positions()
    assert len(poly_pos) == 1 and poly_pos[0].outcome == "YES"
    assert len(kalshi_pos) == 1 and kalshi_pos[0].outcome == "NO"
    assert await poly.get_balance() < 1000.0  # 余额被扣
    assert result.realized_profit_usd == pytest.approx(15.0)
    assert all(leg.order_id is not None for leg in result.legs)


async def test_dry_run_records_intended_orders() -> None:
    """dry-run（无适配器=非全模拟盘）notes 列出每条腿「将要下的单」。"""
    clock = FakeClock()
    engine = ExecutionEngine(adapters={}, clock=clock)  # 无适配器 → 非全模拟盘 → 走演练
    plan = engine.build_plan(make_opportunity(poly_price=0.40, kalshi_price=0.45), size_usd=100.0)

    result = await engine.execute_plan(plan, dry_run=True)

    assert result.status is TradePlanStatus.COMPLETED
    assert "polymarket 买 YES" in result.notes
    assert "kalshi 买 NO" in result.notes
    assert engine.has_residual_exposure(result) is False


# --------------------------------------------------------------------------- #
# 已实现收益核算（任务 2：检查每笔交易是否真实有收益）
# --------------------------------------------------------------------------- #

async def test_realized_pnl_profitable_arbitrage() -> None:
    """双腿按目标价全额成交：核算已实现收益（赔付 N×$1 − 成本）。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=10_000.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=10_000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)
    # 每对成本 0.40 + 0.45 = 0.85 < 1 → 真实套利。预算 85 → N = 100 合约。
    plan = engine.build_plan(make_opportunity(poly_price=0.40, kalshi_price=0.45), size_usd=85.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.COMPLETED
    # N = 85 / 0.85 = 100；赔付 = 100×$1 = 100；成本 = 100×0.40 + 100×0.45 = 85。
    assert result.expected_payoff_usd == pytest.approx(100.0)
    assert result.filled_cost_usd == pytest.approx(85.0)
    assert result.realized_profit_usd == pytest.approx(15.0)
    assert result.realized_profit_margin == pytest.approx(15.0 / 85.0, abs=1e-5)
    assert result.is_genuinely_profitable is True


async def test_realized_pnl_unprofitable_due_to_slippage() -> None:
    """关键：检测时为正的套利，因滑点按更差价成交后实际亏损 → 应被识别为不盈利。"""
    clock = FakeClock()

    # 两腿都以远高于限价的价格成交（滑点），使总成本 > 赔付。
    def slip(limit_price: float, quantity: float):
        return quantity, min(1.0, limit_price + 0.30)  # 每腿 +0.30 滑点

    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=10_000.0, clock=clock, fill_policy=slip)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=10_000.0, clock=clock, fill_policy=slip)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)
    # 检测价 0.40+0.45=0.85（看似套利），但实际成交 0.70+0.75=1.45 > 1 → 亏损。
    plan = engine.build_plan(make_opportunity(poly_price=0.40, kalshi_price=0.45), size_usd=85.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.COMPLETED  # 双腿都成交了
    # N = 100；赔付 100；实际成本 = 100×0.70 + 100×0.75 = 145 → 收益 = 100 - 145 = -45。
    assert result.filled_cost_usd == pytest.approx(145.0)
    assert result.realized_profit_usd == pytest.approx(-45.0)
    assert result.is_genuinely_profitable is False  # 检测为正、实际亏损 —— 正是要抓的情形


async def test_dry_run_has_no_realized_pnl() -> None:
    """dry-run 演练不产生真实成交，故无已实现收益核算。"""
    engine = ExecutionEngine(adapters={}, clock=FakeClock())
    plan = engine.build_plan(make_opportunity(), size_usd=85.0)
    result = await engine.execute_plan(plan, dry_run=True)
    assert result.realized_profit_usd is None
    assert result.is_genuinely_profitable is None


async def test_filled_orders_exposed_for_persistence() -> None:
    """执行后已成交订单经 last_orders 暴露，供调用方持久化对账。"""
    clock = FakeClock()
    poly = PaperExecutionAdapter(name="polymarket", starting_balance_usd=10_000.0, clock=clock)
    kalshi = PaperExecutionAdapter(name="kalshi", starting_balance_usd=10_000.0, clock=clock)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)
    plan = engine.build_plan(make_opportunity(), size_usd=85.0)
    await engine.execute_plan(plan)
    orders = engine.last_orders.get(plan.plan_id, [])
    assert len(orders) == 2
    assert all(o.filled_quantity > 0 for o in orders)


# --------------------------------------------------------------------------- #
# leg risk 执行改进：先难后易排序 + 全成或撤（IOC）
# --------------------------------------------------------------------------- #
class RecordingAdapter:
    """记录下单顺序与撤单调用的假执行适配器，可配置成交行为。

    - ``order_log`` 为共享列表，按 ``place_order`` 实际被调用的顺序追加平台名，
      用于断言「先难后易」的执行次序。
    - ``buy_fills`` 控制买单是全成（FILLED）还是部分成交（PARTIALLY_FILLED）。
    - 记录 ``cancelled`` 平台订单号，用于断言 IOC 撤单。
    - 卖出（补救平仓）始终全成，使残余敞口判断聚焦在被测路径上。
    """

    def __init__(
        self,
        name: str,
        clock: FakeClock,
        order_log: List[str],
        *,
        buy_fills: bool = True,
    ) -> None:
        self.name = name
        self._clock = clock
        self._seq = 0
        self.order_log = order_log
        self.cancelled: List[str] = []
        self.buy_fills = buy_fills
        self.is_paper = False

    async def place_order(
        self,
        *,
        market_id: str,
        outcome: str,
        side: OrderSide,
        limit_price: float,
        quantity: float,
    ) -> Order:
        self._seq += 1
        now = self._clock()
        oid = f"{self.name}-{self._seq}"
        if side is OrderSide.BUY:
            self.order_log.append(self.name)
            if self.buy_fills:
                filled, status = quantity, OrderStatus.FILLED
            else:
                filled, status = quantity / 2.0, OrderStatus.PARTIALLY_FILLED
        else:  # 补救平仓（卖出）始终全成
            filled, status = quantity, OrderStatus.FILLED
        return Order(
            order_id=oid,
            platform=self.name,
            market_id=market_id,
            outcome=outcome,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            status=status,
            platform_order_id=oid,
            filled_quantity=filled,
            avg_fill_price=limit_price,
            created_at=now,
            updated_at=now,
        )

    async def cancel_order(self, platform_order_id: str) -> Order:
        self.cancelled.append(platform_order_id)
        now = self._clock()
        return Order(
            order_id=platform_order_id,
            platform=self.name,
            market_id="?",
            outcome="?",
            side=OrderSide.BUY,
            limit_price=0.5,
            quantity=0.0,
            status=OrderStatus.CANCELLED,
            platform_order_id=platform_order_id,
            created_at=now,
            updated_at=now,
        )

    async def get_order(self, platform_order_id: str) -> Order:  # pragma: no cover
        raise ExecutionError("not used", platform=self.name)

    async def get_balance(self) -> float:  # pragma: no cover
        return 0.0

    async def get_positions(self) -> list:  # pragma: no cover
        return []


def _opportunity_with_liquidity(
    *, poly_liq: float, kalshi_liq: float
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        group_id="grp-liq",
        event_title="leg risk 排序测试",
        legs=[
            ArbLeg(
                platform="polymarket",
                market_id="poly-m1",
                outcome="YES",
                price=0.40,
                available_liquidity_usd=poly_liq,
            ),
            ArbLeg(
                platform="kalshi",
                market_id="kalshi-m1",
                outcome="NO",
                price=0.45,
                available_liquidity_usd=kalshi_liq,
            ),
        ],
        net_profit_margin=0.15,
        recommended_size_usd=20.0,
        detected_at=BASE_TIME,
        data_age_seconds=1.0,
    )


async def test_executes_thinnest_liquidity_leg_first():
    """先难后易：流动性更薄的腿（kalshi $20 < poly $500）应先下单。"""
    clock = FakeClock()
    order_log: List[str] = []
    poly = RecordingAdapter("polymarket", clock, order_log)
    kalshi = RecordingAdapter("kalshi", clock, order_log)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    opp = _opportunity_with_liquidity(poly_liq=500.0, kalshi_liq=20.0)
    plan = engine.build_plan(opp, size_usd=20.0)
    result = await engine.execute_plan(plan)

    # 两腿都全成 → COMPLETED；且 kalshi（更薄）先于 polymarket 被下单。
    assert result.status is TradePlanStatus.COMPLETED
    assert order_log == ["kalshi", "polymarket"]


async def test_leg_build_plan_propagates_liquidity():
    """build_plan 应把每腿的可成交流动性带入 TradeLeg（供排序用）。"""
    engine = ExecutionEngine(adapters={}, clock=FakeClock())
    opp = _opportunity_with_liquidity(poly_liq=500.0, kalshi_liq=20.0)
    plan = engine.build_plan(opp, size_usd=20.0)
    liq = {leg.platform: leg.available_liquidity_usd for leg in plan.legs}
    assert liq == {"polymarket": 500.0, "kalshi": 20.0}


async def test_partial_fill_remainder_is_cancelled_ioc():
    """全成或撤：第二腿部分成交时，应立即撤掉其未成交挂单（IOC），并触发补救。

    构造：poly（更薄，先下）买入全成；kalshi（较厚，后下）只部分成交 → 引擎对 kalshi
    订单调用 cancel_order 撤掉剩余，再反向平掉已成交的 poly 腿，计划 FAILED。
    """
    clock = FakeClock()
    order_log: List[str] = []
    poly = RecordingAdapter("polymarket", clock, order_log, buy_fills=True)
    kalshi = RecordingAdapter("kalshi", clock, order_log, buy_fills=False)  # 部分成交
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    # poly 更薄 → 先下并全成；kalshi 较厚 → 后下且部分成交。
    opp = _opportunity_with_liquidity(poly_liq=20.0, kalshi_liq=500.0)
    plan = engine.build_plan(opp, size_usd=20.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    # IOC：kalshi 的部分成交订单的剩余被撤单。
    assert kalshi.cancelled == ["kalshi-1"]
    # 已成交的 poly 腿被反向平仓（无残余敞口）。
    assert engine.has_residual_exposure(result) is False


async def test_first_leg_partial_fill_killed_and_no_second_leg():
    """最难成交的腿（先下）部分成交即被撤单，且不再下第二腿（避免裸敞口）。"""
    clock = FakeClock()
    order_log: List[str] = []
    # kalshi 更薄 → 先下且部分成交；poly 较厚 → 本不该被下单。
    poly = RecordingAdapter("polymarket", clock, order_log, buy_fills=True)
    kalshi = RecordingAdapter("kalshi", clock, order_log, buy_fills=False)
    engine = ExecutionEngine(adapters={"polymarket": poly, "kalshi": kalshi}, clock=clock)

    opp = _opportunity_with_liquidity(poly_liq=500.0, kalshi_liq=20.0)
    plan = engine.build_plan(opp, size_usd=20.0)
    result = await engine.execute_plan(plan)

    assert result.status is TradePlanStatus.FAILED
    # 只下了 kalshi（最薄、先下）这一腿；poly 未被下单（买单日志里没有 polymarket）。
    assert order_log == ["kalshi"]
    # kalshi 部分成交剩余被 IOC 撤单。
    assert kalshi.cancelled == ["kalshi-1"]
