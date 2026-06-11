"""Canonical data models for the Prediction Market Arbitrage Scanner.

These pydantic models define the normalized representation every downstream
component consumes. Validators enforce the core invariants from the design's
correctness properties:

- Property 1 (Price bounds): every price/bid/ask is within [0, 1].
- Property 2 (Non-negative magnitudes): volume/liquidity are None or >= 0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class FieldStatus(str, Enum):
    """Per-field availability of a canonical market value (Req 2.4)."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


class Outcome(BaseModel):
    """A single tradable result within a market (e.g. "YES" / "NO").

    Prices are implied probabilities in [0, 1] (Req 2.2). The optional bid/ask
    capture the top of book so the arbitrage engine can account for the cost of
    crossing the spread.
    """

    name: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    available_liquidity_usd: Optional[float] = None

    @field_validator("price", "bid", "ask")
    @classmethod
    def _price_in_unit_interval(cls, value: Optional[float]) -> Optional[float]:
        # Property 1: prices are implied probabilities and must lie in [0, 1].
        if value is None:
            return value
        if value < 0 or value > 1:
            raise ValueError("price/bid/ask must be within [0, 1]")
        return value

    @field_validator("available_liquidity_usd")
    @classmethod
    def _liquidity_non_negative(cls, value: Optional[float]) -> Optional[float]:
        # Property 2: monetary magnitudes are never negative.
        if value is None:
            return value
        if value < 0:
            raise ValueError("available_liquidity_usd must be >= 0")
        return value


class CanonicalMarket(BaseModel):
    """Normalized representation of a single market from one platform (Req 2.1)."""

    platform: str
    market_id: str
    title: str
    outcomes: List[Outcome] = Field(default_factory=list)
    volume_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    fee_rate: Optional[float] = None
    retrieved_at: datetime
    field_status: Dict[str, FieldStatus] = Field(default_factory=dict)
    unavailable_reasons: Dict[str, str] = Field(default_factory=dict)
    is_stale: bool = False
    # 市场类目（如 politics/sports/crypto）。用于按类目筛选/展示；平台无此概念时为 None。
    category: Optional[str] = None
    # 结算/到期日期（UTC）。这是 Tier-0 红线 C「结算等价」的关键维度：标题几乎相同
    # 但结算日期不同的市场是**不同事件**（例如同一事件下「by April 30」与「before 2027」
    # 两个子市场，标题文本相同、结算窗口不同）。匹配引擎据此做日期硬 veto，避免跨不同
    # 结算窗口的错配。平台未提供或无法解析时为 None（此时日期维度按中性处理，不否决）。
    resolution_date: Optional[datetime] = None
    # 平台自报的跨平台关联线索（Phase Three）：键为对端平台名，值为对端市场标识列表。
    # 例如 predict.fun 的市场会标注其对应的 Polymarket conditionId / Kalshi ticker。
    # 匹配引擎可用作「金标准」关联，比纯标题语义更可靠。默认空，向后兼容。
    cross_refs: Dict[str, List[str]] = Field(default_factory=dict)

    @field_validator("volume_usd", "liquidity_usd")
    @classmethod
    def _magnitude_non_negative(cls, value: Optional[float]) -> Optional[float]:
        # Property 2: volume/liquidity are None/unavailable or >= 0.
        if value is None:
            return value
        if value < 0:
            raise ValueError("volume_usd/liquidity_usd must be >= 0")
        return value

    @field_validator("fee_rate")
    @classmethod
    def _fee_rate_in_unit_interval(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if value < 0 or value > 1:
            raise ValueError("fee_rate must be within [0, 1]")
        return value

    @property
    def age_seconds(self) -> float:
        """Age of the underlying data in seconds (Req 8.1, Property 3).

        Computed against the current UTC wall clock. ``retrieved_at`` may be
        naive (assumed UTC) or timezone-aware.
        """
        retrieved = self.retrieved_at
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - retrieved).total_seconds()


class OutcomeAlignment(BaseModel):
    """Maps a canonical outcome to each platform's native outcome (Req 4.3).

    ``inverted`` is True when a platform phrases the outcome as the negation of
    the canonical outcome (e.g. one platform's NO maps to canonical YES).
    """

    canonical_outcome: str
    platform_outcomes: Dict[str, str] = Field(default_factory=dict)
    inverted: Dict[str, bool] = Field(default_factory=dict)


class EquivalentMarketGroup(BaseModel):
    """A set of markets from different platforms representing one event (Req 4.2)."""

    group_id: str
    members: List[CanonicalMarket] = Field(default_factory=list)
    outcome_map: List[OutcomeAlignment] = Field(default_factory=list)
    match_confidence: float

    @field_validator("match_confidence")
    @classmethod
    def _confidence_in_unit_interval(cls, value: float) -> float:
        # Property 7: match_confidence is in [0, 1] (Req 4.4).
        if value < 0 or value > 1:
            raise ValueError("match_confidence must be within [0, 1]")
        return value


class ArbLeg(BaseModel):
    """One side of an arbitrage opportunity: buy an outcome on a platform."""

    platform: str
    market_id: str
    outcome: str
    side: str = "buy"
    price: float

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("price must be within [0, 1]")
        return value


class ArbitrageOpportunity(BaseModel):
    """A detected cross-platform arbitrage condition (Req 5.4)."""

    group_id: str
    event_title: str
    legs: List[ArbLeg] = Field(default_factory=list)
    net_profit_margin: float
    recommended_size_usd: float
    detected_at: datetime
    data_age_seconds: float

    @field_validator("recommended_size_usd")
    @classmethod
    def _size_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("recommended_size_usd must be >= 0")
        return value


# ---------------------------------------------------------------------------
# 信号生命周期模型（Phase Two · 切片 A）
#
# 把无状态的瞬时 ArbitrageOpportunity 提升为有状态的 Signal：一个套利信号从
# 首次检测（OPEN）→ 持续存在（SUSTAINED）→ 价差消失/失效（CLOSED）的完整生命
# 周期，并记录首次/最近检测时间、峰值净利润率、持续时长。SignalEvent 是该生命
# 周期产生的事件（OPENED / UPDATED / CLOSED），作为信号工具可被消费、回放、审计
# 的核心产物。
# ---------------------------------------------------------------------------


class SignalStatus(str, Enum):
    """套利信号的生命周期状态。"""

    OPEN = "open"            # 首次检测到（本周期新出现）
    SUSTAINED = "sustained"  # 持续存在（此前已检测到，本周期仍在）
    CLOSED = "closed"        # 价差消失/失效，信号关闭


class SignalEventType(str, Enum):
    """信号事件的类型。"""

    OPENED = "opened"
    UPDATED = "updated"
    CLOSED = "closed"


class Signal(BaseModel):
    """一个有状态的套利信号，封装某个机会组在时间维度上的生命周期。

    以 ``group_id`` 标识。``status`` 随每个流水线周期的对账而转移：首次出现为
    ``OPEN``，后续仍在为 ``SUSTAINED``，从机会快照中消失则为 ``CLOSED``。
    """

    group_id: str
    event_title: str
    status: SignalStatus
    legs: List[ArbLeg] = Field(default_factory=list)
    # 最近一次评估的净利润率与建议规模（来自当周期的机会快照）。
    net_profit_margin: float
    recommended_size_usd: float
    data_age_seconds: float
    # 生命周期时间戳。
    first_detected_at: datetime
    last_seen_at: datetime
    closed_at: Optional[datetime] = None
    # 生命周期内观测到的峰值净利润率（单调不减，直到关闭）。
    peak_net_profit_margin: float

    @property
    def duration_seconds(self) -> float:
        """信号已持续的时长（秒）：从首次检测到最近一次出现（或关闭）。"""
        end = self.closed_at if self.closed_at is not None else self.last_seen_at
        start = self.first_detected_at
        end_aware = end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)
        start_aware = (
            start if start.tzinfo is not None else start.replace(tzinfo=timezone.utc)
        )
        return (end_aware - start_aware).total_seconds()

    @property
    def is_active(self) -> bool:
        """信号是否仍然活跃（未关闭）。"""
        return self.status is not SignalStatus.CLOSED


class SignalEvent(BaseModel):
    """信号生命周期中产生的一个事件，构成可消费/回放/审计的事件流。"""

    event_type: SignalEventType
    group_id: str
    event_title: str
    status: SignalStatus
    net_profit_margin: float
    recommended_size_usd: float
    peak_net_profit_margin: float
    duration_seconds: float
    occurred_at: datetime


# ---------------------------------------------------------------------------
# 交易域模型（Phase Three · 切片 E）
#
# 把「检测到的套利机会」推进到「可执行的交易」需要一组交易域模型：订单、成交、
# 持仓，以及把一个套利机会落成双腿下单计划的 TradePlan。这些是执行半边
# （ExecutionAdapter / ExecutionEngine / RiskManager）共同消费的契约。
#
# 安全注记：这些模型只描述交易意图与状态，不含任何密钥/私钥；鉴权凭证只存在于
# 执行适配器内部（经环境变量注入），绝不进入这些模型，因而也不会入库/入日志/入响应。
# ---------------------------------------------------------------------------


class OrderSide(str, Enum):
    """订单方向。Phase Three 套利只买入互补结果，故主用 BUY。"""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """订单生命周期状态。"""

    PENDING = "pending"              # 已创建，未提交
    SUBMITTED = "submitted"          # 已提交平台，待成交
    FILLED = "filled"                # 全部成交
    PARTIALLY_FILLED = "partially_filled"  # 部分成交
    FAILED = "failed"                # 提交失败/被拒
    CANCELLED = "cancelled"          # 已撤销


class TradePlanStatus(str, Enum):
    """一个套利双腿计划的整体状态。"""

    PENDING_CONFIRMATION = "pending_confirmation"  # 待人工确认
    CONFIRMED = "confirmed"          # 已确认，待执行
    REJECTED = "rejected"            # 人工拒绝
    EXECUTING = "executing"          # 执行中
    COMPLETED = "completed"          # 双腿均成交
    FAILED = "failed"                # 执行失败（含一腿失败的残余敞口情形）


class Order(BaseModel):
    """一笔下单意图及其状态（执行半边的基本单位）。"""

    order_id: str                    # 本系统内的订单标识
    platform: str
    market_id: str
    outcome: str                     # 目标结果名（平台原生名）
    side: OrderSide = OrderSide.BUY
    limit_price: float               # 限价（隐含概率 0..1）
    quantity: float                  # 合约数量
    status: OrderStatus = OrderStatus.PENDING
    platform_order_id: Optional[str] = None  # 平台返回的订单号
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    reason: Optional[str] = None     # 失败/撤销原因

    @field_validator("limit_price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("limit_price must be within [0, 1]")
        return value

    @field_validator("quantity", "filled_quantity")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quantity/filled_quantity must be >= 0")
        return value


class Fill(BaseModel):
    """一笔成交回报。"""

    order_id: str
    platform: str
    quantity: float
    price: float
    fee: float = 0.0
    filled_at: datetime

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("price must be within [0, 1]")
        return value


class Position(BaseModel):
    """某平台某市场某结果上的当前持仓。"""

    platform: str
    market_id: str
    outcome: str
    quantity: float                  # 持有合约数（净）
    avg_price: float                 # 平均建仓价
    updated_at: datetime


class TradeLeg(BaseModel):
    """套利计划中的一腿：在某平台买入某结果。"""

    platform: str
    market_id: str
    outcome: str
    side: OrderSide = OrderSide.BUY
    target_price: float              # 期望成交价（来自信号的 ask 价）
    quantity: float
    order_id: Optional[str] = None   # 关联的订单（执行后填充）

    @field_validator("target_price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("target_price must be within [0, 1]")
        return value


class TradePlan(BaseModel):
    """由一个套利机会落成的双腿（或多腿）下单计划。

    一个计划对应一个 `EquivalentMarketGroup` 的套利机会：在不同平台买入互补结果。
    它从 `PENDING_CONFIRMATION` 开始，经人工确认后执行，目标是两腿都成交
    （`COMPLETED`）；任一腿失败进入 `FAILED` 并触发补救（撤销/对冲已成交腿）。
    """

    plan_id: str
    group_id: str
    event_title: str
    legs: List[TradeLeg] = Field(default_factory=list)
    expected_net_profit_margin: float
    size_usd: float                  # 计划投入资金（受流动性/风控限制）
    status: TradePlanStatus = TradePlanStatus.PENDING_CONFIRMATION
    created_at: datetime
    updated_at: datetime
    notes: Optional[str] = None      # 风控/执行/补救备注
    # 执行后的真实成交核算（Phase 3 · 切片 H/I）。基于**实际成交价**而非检测报价，
    # 用于审计「每笔交易是否真实有收益」——检测时为正的套利，按实际成交价可能因
    # 滑点/费用而不再盈利。仅在执行（非 dry-run）后填充。
    filled_cost_usd: Optional[float] = None       # 实际成交总成本（Σ 成交量×成交价 + 费用）
    expected_payoff_usd: Optional[float] = None   # 结算赔付（对冲合约数 × $1）
    realized_profit_usd: Optional[float] = None   # 已实现收益 = 赔付 − 成本
    realized_profit_margin: Optional[float] = None  # 已实现收益率 = 收益 / 成本

    @field_validator("size_usd")
    @classmethod
    def _size_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("size_usd must be >= 0")
        return value

    @property
    def is_genuinely_profitable(self) -> Optional[bool]:
        """按实际成交价，这笔交易是否真实有正收益。未执行核算时为 None。"""
        if self.realized_profit_usd is None:
            return None
        return self.realized_profit_usd > 0
