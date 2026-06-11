"""The Read API (FastAPI) — the stable interface the User and future aggregator consume.

This module exposes the scanner's current snapshot over HTTP (Req 7.4):

| Method   | Path             | Purpose                                                       | Requirements        |
|----------|------------------|---------------------------------------------------------------|---------------------|
| GET      | /markets         | Ranked markets; ``sort``, ``min_volume``, ``min_liquidity``   | 3.3, 3.4, 3.5, 8.1  |
| GET      | /groups          | Equivalent market groups; ``min_confidence``                  | 4.4, 4.5            |
| GET      | /opportunities   | Current opportunities, sorted by net margin desc; ``min_margin`` | 5.5, 6.1, 8.1    |
| GET      | /health          | Per-adapter status, last successful cycle, counts             | 1.5                 |
| GET/PUT  | /config/alerts   | View/update alert criteria                                    | 6.2                 |

Every market/opportunity response carries ``data_age_seconds`` and ``is_stale``
so callers can judge freshness (Req 8.1). ``/opportunities`` upholds Property 10:
the listing is sorted by ``net_profit_margin`` descending and never contains an
opportunity with ``net_profit_margin <= 0``.

The app is built by :func:`create_app`, an application factory that takes the
stores/services via dependency injection. This keeps the API free of global
state and lets tests seed an in-memory store and drive it with a
``TestClient``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

# 仪表盘单页 HTML 的路径（与本模块同目录下的 static/）。
_DASHBOARD_HTML = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")

from scanner.config import (
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    AlertConfig,
    AlertCriteriaConfig,
)
from scanner.matching import MatchingEngine
from scanner.models import (
    ArbitrageOpportunity,
    CanonicalMarket,
    EquivalentMarketGroup,
    Outcome,
    OutcomeAlignment,
    Signal,
    SignalEvent,
    TradeLeg,
    TradePlan,
    TradePlanStatus,
)
from scanner.opportunities import OpportunityService
from scanner.observability import PipelineMetrics
from scanner.ranking import RankingService
from scanner.signals import SignalStore
from scanner.store import MarketStore, OpportunityStore


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class OutcomeResponse(BaseModel):
    """A single outcome as returned by the API."""

    name: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    available_liquidity_usd: Optional[float] = None

    @classmethod
    def from_model(cls, outcome: Outcome) -> "OutcomeResponse":
        return cls(
            name=outcome.name,
            price=outcome.price,
            bid=outcome.bid,
            ask=outcome.ask,
            available_liquidity_usd=outcome.available_liquidity_usd,
        )


class MarketResponse(BaseModel):
    """A canonical market with freshness metadata (Req 8.1)."""

    platform: str
    market_id: str
    title: str
    outcomes: List[OutcomeResponse] = Field(default_factory=list)
    volume_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    fee_rate: Optional[float] = None
    category: Optional[str] = None
    resolution_date: Optional[datetime] = None
    cross_refs: Dict[str, List[str]] = Field(default_factory=dict)
    retrieved_at: datetime
    is_stale: bool
    data_age_seconds: float

    @classmethod
    def from_model(cls, market: CanonicalMarket) -> "MarketResponse":
        # Req 8.1: surface the age of the underlying data and its staleness.
        return cls(
            platform=market.platform,
            market_id=market.market_id,
            title=market.title,
            outcomes=[OutcomeResponse.from_model(o) for o in market.outcomes],
            volume_usd=market.volume_usd,
            liquidity_usd=market.liquidity_usd,
            fee_rate=market.fee_rate,
            category=market.category,
            resolution_date=market.resolution_date,
            cross_refs=dict(market.cross_refs),
            retrieved_at=market.retrieved_at,
            is_stale=market.is_stale,
            data_age_seconds=market.age_seconds,
        )


class OutcomeAlignmentResponse(BaseModel):
    """How a canonical outcome maps to each platform's native outcome."""

    canonical_outcome: str
    platform_outcomes: Dict[str, str] = Field(default_factory=dict)
    inverted: Dict[str, bool] = Field(default_factory=dict)

    @classmethod
    def from_model(cls, alignment: OutcomeAlignment) -> "OutcomeAlignmentResponse":
        return cls(
            canonical_outcome=alignment.canonical_outcome,
            platform_outcomes=dict(alignment.platform_outcomes),
            inverted=dict(alignment.inverted),
        )


class GroupResponse(BaseModel):
    """An equivalent market group with its confidence (Req 4.4)."""

    group_id: str
    members: List[MarketResponse] = Field(default_factory=list)
    outcome_map: List[OutcomeAlignmentResponse] = Field(default_factory=list)
    match_confidence: float

    @classmethod
    def from_model(cls, group: EquivalentMarketGroup) -> "GroupResponse":
        return cls(
            group_id=group.group_id,
            members=[MarketResponse.from_model(m) for m in group.members],
            outcome_map=[OutcomeAlignmentResponse.from_model(a) for a in group.outcome_map],
            match_confidence=group.match_confidence,
        )


class ArbLegResponse(BaseModel):
    """One leg of an arbitrage opportunity."""

    platform: str
    market_id: str
    outcome: str
    side: str
    price: float


class OpportunityResponse(BaseModel):
    """An arbitrage opportunity with freshness metadata (Req 8.1)."""

    group_id: str
    event_title: str
    legs: List[ArbLegResponse] = Field(default_factory=list)
    net_profit_margin: float
    recommended_size_usd: float
    detected_at: datetime
    data_age_seconds: float
    is_stale: bool
    # 扣 gas 后的真实美元利润核算（缺口 E-1）。未配置 gas 估算时为 None（诚实表示「未建模」）。
    gas_cost_usd: Optional[float] = None
    net_profit_after_gas_usd: Optional[float] = None

    @classmethod
    def from_model(
        cls, opp: ArbitrageOpportunity, *, staleness_threshold: float
    ) -> "OpportunityResponse":
        # Req 8.1: an opportunity is stale when its underlying price data has
        # aged past the staleness threshold.
        return cls(
            group_id=opp.group_id,
            event_title=opp.event_title,
            legs=[
                ArbLegResponse(
                    platform=leg.platform,
                    market_id=leg.market_id,
                    outcome=leg.outcome,
                    side=leg.side,
                    price=leg.price,
                )
                for leg in opp.legs
            ],
            net_profit_margin=opp.net_profit_margin,
            recommended_size_usd=opp.recommended_size_usd,
            detected_at=opp.detected_at,
            data_age_seconds=opp.data_age_seconds,
            is_stale=opp.data_age_seconds > staleness_threshold,
            gas_cost_usd=opp.gas_cost_usd,
            net_profit_after_gas_usd=opp.net_profit_after_gas_usd,
        )


class SignalResponse(BaseModel):
    """一个有状态套利信号的 API 表示（Phase Two · 切片 A）。"""

    group_id: str
    event_title: str
    status: str
    legs: List[ArbLegResponse] = Field(default_factory=list)
    net_profit_margin: float
    recommended_size_usd: float
    peak_net_profit_margin: float
    data_age_seconds: float
    first_detected_at: datetime
    last_seen_at: datetime
    closed_at: Optional[datetime] = None
    duration_seconds: float

    @classmethod
    def from_model(cls, signal: Signal) -> "SignalResponse":
        return cls(
            group_id=signal.group_id,
            event_title=signal.event_title,
            status=signal.status.value,
            legs=[
                ArbLegResponse(
                    platform=leg.platform,
                    market_id=leg.market_id,
                    outcome=leg.outcome,
                    side=leg.side,
                    price=leg.price,
                )
                for leg in signal.legs
            ],
            net_profit_margin=signal.net_profit_margin,
            recommended_size_usd=signal.recommended_size_usd,
            peak_net_profit_margin=signal.peak_net_profit_margin,
            data_age_seconds=signal.data_age_seconds,
            first_detected_at=signal.first_detected_at,
            last_seen_at=signal.last_seen_at,
            closed_at=signal.closed_at,
            duration_seconds=signal.duration_seconds,
        )


class SignalEventResponse(BaseModel):
    """信号事件流中一个事件的 API 表示。"""

    event_type: str
    group_id: str
    event_title: str
    status: str
    net_profit_margin: float
    recommended_size_usd: float
    peak_net_profit_margin: float
    duration_seconds: float
    occurred_at: datetime

    @classmethod
    def from_model(cls, event: SignalEvent) -> "SignalEventResponse":
        return cls(
            event_type=event.event_type.value,
            group_id=event.group_id,
            event_title=event.event_title,
            status=event.status.value,
            net_profit_margin=event.net_profit_margin,
            recommended_size_usd=event.recommended_size_usd,
            peak_net_profit_margin=event.peak_net_profit_margin,
            duration_seconds=event.duration_seconds,
            occurred_at=event.occurred_at,
        )


class TradeLegResponse(BaseModel):
    """交易计划中一腿的 API 表示（Phase 3 · 切片 H）。"""

    platform: str
    market_id: str
    outcome: str
    side: str
    target_price: float
    quantity: float
    order_id: Optional[str] = None

    @classmethod
    def from_model(cls, leg: TradeLeg) -> "TradeLegResponse":
        return cls(
            platform=leg.platform,
            market_id=leg.market_id,
            outcome=leg.outcome,
            side=leg.side.value,
            target_price=leg.target_price,
            quantity=leg.quantity,
            order_id=leg.order_id,
        )


class TradePlanResponse(BaseModel):
    """一个双腿交易计划的 API 表示（Phase 3 · 切片 H）。"""

    plan_id: str
    group_id: str
    event_title: str
    legs: List[TradeLegResponse] = Field(default_factory=list)
    expected_net_profit_margin: float
    size_usd: float
    status: str
    created_at: datetime
    updated_at: datetime
    notes: Optional[str] = None
    filled_cost_usd: Optional[float] = None
    expected_payoff_usd: Optional[float] = None
    realized_profit_usd: Optional[float] = None
    realized_profit_margin: Optional[float] = None
    genuinely_profitable: Optional[bool] = None

    @classmethod
    def from_model(cls, plan: TradePlan) -> "TradePlanResponse":
        return cls(
            plan_id=plan.plan_id,
            group_id=plan.group_id,
            event_title=plan.event_title,
            legs=[TradeLegResponse.from_model(leg) for leg in plan.legs],
            expected_net_profit_margin=plan.expected_net_profit_margin,
            size_usd=plan.size_usd,
            status=plan.status.value,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            notes=plan.notes,
            filled_cost_usd=plan.filled_cost_usd,
            expected_payoff_usd=plan.expected_payoff_usd,
            realized_profit_usd=plan.realized_profit_usd,
            realized_profit_margin=plan.realized_profit_margin,
            genuinely_profitable=plan.is_genuinely_profitable,
        )


class AdapterHealth(BaseModel):
    """Per-adapter health (Req 1.5): status, last successful cycle, counts."""

    name: str
    healthy: bool
    market_count: int
    last_successful_cycle: Optional[datetime] = None
    last_error: Optional[str] = None
    # 数据接入韧性指标（Phase Two · 切片 C）。
    circuit_state: Optional[str] = None
    success_count: Optional[int] = None
    failure_count: Optional[int] = None


class HealthResponse(BaseModel):
    """Overall service health with per-adapter breakdown."""

    status: str
    market_count: int
    opportunity_count: int
    adapters: List[AdapterHealth] = Field(default_factory=list)


# A health provider returns the per-adapter health snapshot. When not supplied,
# the API derives a best-effort view from the market store.
HealthProvider = Callable[[], List[AdapterHealth]]


def _derive_adapter_health(store: MarketStore) -> List[AdapterHealth]:
    """Best-effort per-adapter health derived from the current market snapshot.

    Markets are grouped by platform; an adapter is considered healthy when it
    has at least one non-stale market. ``last_successful_cycle`` is the most
    recent ``retrieved_at`` across that platform's markets.
    """
    by_platform: Dict[str, List[CanonicalMarket]] = {}
    for market in store.list_all():
        by_platform.setdefault(market.platform, []).append(market)

    adapters: List[AdapterHealth] = []
    for platform in sorted(by_platform):
        markets = by_platform[platform]
        last_cycle = max((m.retrieved_at for m in markets), default=None)
        healthy = any(not m.is_stale for m in markets)
        adapters.append(
            AdapterHealth(
                name=platform,
                healthy=healthy,
                market_count=len(markets),
                last_successful_cycle=last_cycle,
            )
        )
    return adapters


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    market_store: MarketStore,
    opportunity_service: Optional[OpportunityService] = None,
    opportunity_store: Optional[OpportunityStore] = None,
    ranking_service: Optional[RankingService] = None,
    matching_engine: Optional[MatchingEngine] = None,
    alert_config: Optional[AlertConfig] = None,
    health_provider: Optional[HealthProvider] = None,
    signal_store: Optional[SignalStore] = None,
    metrics: Optional["PipelineMetrics"] = None,
    history_store: Optional[object] = None,
    trading_service: Optional[object] = None,
    trade_api_key: Optional[str] = None,
    onchain_balances: Optional[Dict[str, Dict[str, object]]] = None,
    staleness_threshold: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
) -> FastAPI:
    """Build the Read API over injected stores and services (Req 7.4).

    Args:
        market_store: Source of current canonical markets for ``/markets``,
            ``/groups``, and the default health view.
        opportunity_service: Provides the filtered, sorted opportunity listing
            for ``/opportunities`` (Property 10). If omitted, one is built
            around ``opportunity_store`` (or a fresh store).
        opportunity_store: Used to construct an ``OpportunityService`` when one
            is not supplied directly.
        ranking_service: Ranks/filters markets for ``/markets``. Defaults to a
            fresh ``RankingService``.
        matching_engine: Produces equivalent market groups for ``/groups`` from
            the current markets. Defaults to a fresh ``MatchingEngine``.
        alert_config: The alert configuration exposed and mutated by
            ``/config/alerts``. Defaults to a fresh ``AlertConfig``.
        health_provider: Optional callable returning per-adapter health for
            ``/health``. Defaults to a view derived from ``market_store``.
        staleness_threshold: Age in seconds beyond which an opportunity's data
            is reported ``is_stale`` (Req 8.1, 8.3).

    Returns:
        A configured ``FastAPI`` application.
    """
    if opportunity_service is None:
        opportunity_service = OpportunityService(
            store=opportunity_store or OpportunityStore()
        )
    ranker = ranking_service or RankingService()
    matcher = matching_engine or MatchingEngine()
    alerts = alert_config or AlertConfig()

    app = FastAPI(title="Prediction Market Arbitrage Scanner", version="0.2.0")

    def _require_trade_auth(x_api_key: Optional[str] = Header(default=None)) -> None:
        """交易（写）端点鉴权依赖。

        ``trade_api_key`` 未配置时**放行**（本机自用阶段，与只读 API 一致的零摩擦）；
        配置后，交易端点必须在 ``X-API-Key`` 头携带正确密钥，否则 401。只读端点不受
        影响。真实下单（切片 I/N）前应配置该密钥。
        """
        if trade_api_key is None:
            return
        if x_api_key != trade_api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        """可视化仪表盘（单页 HTML，定时轮询只读 API）。"""
        if os.path.exists(_DASHBOARD_HTML):
            return FileResponse(_DASHBOARD_HTML, media_type="text/html")
        return HTMLResponse(
            "<h1>Dashboard not found</h1><p>缺少 scanner/static/dashboard.html</p>",
            status_code=404,
        )

    @app.get("/markets", response_model=List[MarketResponse])
    def list_markets(
        sort: str = Query("volume", pattern="^(volume|liquidity)$"),
        min_volume: Optional[float] = Query(None, ge=0),
        min_liquidity: Optional[float] = Query(None, ge=0),
    ) -> List[MarketResponse]:
        """Ranked markets with freshness metadata (Req 3.3, 3.4, 3.5, 8.1)."""
        ranked = ranker.rank(
            market_store.list_all(),
            by=sort,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
        )
        return [MarketResponse.from_model(m) for m in ranked]

    @app.get("/markets/{platform}/{market_id}", response_model=MarketResponse)
    def get_market(platform: str, market_id: str) -> MarketResponse:
        """单个市场明细，含盘口（各结果 bid/ask/流动性）与新鲜度（Phase 3 · 切片 K）。"""
        market = market_store.get(platform, market_id)
        if market is None:
            raise HTTPException(status_code=404, detail="market not found")
        return MarketResponse.from_model(market)

    @app.get("/groups", response_model=List[GroupResponse])
    def list_groups(
        min_confidence: Optional[float] = Query(None, ge=0, le=1),
    ) -> List[GroupResponse]:
        """Equivalent market groups, optionally above a confidence floor (Req 4.4, 4.5)."""
        groups = matcher.match(market_store.list_all())
        if min_confidence is not None:
            groups = [g for g in groups if g.match_confidence >= min_confidence]
        return [GroupResponse.from_model(g) for g in groups]

    @app.get("/groups/{group_id}", response_model=GroupResponse)
    def get_group(group_id: str) -> GroupResponse:
        """单个匹配组明细，含各成员市场盘口与结果映射（Phase 3 · 切片 K）。"""
        for g in matcher.match(market_store.list_all()):
            if g.group_id == group_id:
                return GroupResponse.from_model(g)
        raise HTTPException(status_code=404, detail="group not found")

    @app.get("/opportunities", response_model=List[OpportunityResponse])
    def list_opportunities(
        min_margin: Optional[float] = Query(None),
    ) -> List[OpportunityResponse]:
        """Current opportunities sorted by net margin desc (Req 5.5, 6.1, Property 10).

        The service listing already excludes non-positive margins and sorts
        descending; an optional ``min_margin`` narrows the result further.
        """
        opportunities = opportunity_service.list()
        if min_margin is not None:
            opportunities = [
                o for o in opportunities if o.net_profit_margin >= min_margin
            ]
        return [
            OpportunityResponse.from_model(o, staleness_threshold=staleness_threshold)
            for o in opportunities
        ]

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Per-adapter status, last successful cycle, and counts (Req 1.5)."""
        adapters = (
            health_provider() if health_provider is not None
            else _derive_adapter_health(market_store)
        )
        market_count = len(market_store.list_all())
        opportunity_count = len(opportunity_service.list())
        # "ok" only when every reporting adapter is healthy; otherwise degraded.
        status = "ok" if adapters and all(a.healthy for a in adapters) else "degraded"
        return HealthResponse(
            status=status,
            market_count=market_count,
            opportunity_count=opportunity_count,
            adapters=adapters,
        )

    @app.get("/config/alerts", response_model=AlertCriteriaConfig)
    def get_alert_config() -> AlertCriteriaConfig:
        """Return the current alert criteria (Req 6.2)."""
        return alerts.criteria

    @app.put("/config/alerts", response_model=AlertCriteriaConfig)
    def put_alert_config(criteria: AlertCriteriaConfig) -> AlertCriteriaConfig:
        """Replace the alert criteria in memory and return the new value (Req 6.2)."""
        alerts.criteria = criteria
        return alerts.criteria

    @app.get("/signals", response_model=List[SignalResponse])
    def list_signals() -> List[SignalResponse]:
        """当前活跃的套利信号及其状态、峰值与持续时长（Phase Two · 切片 A）。

        按净利润率降序返回；信号存储未配置时返回空列表。
        """
        if signal_store is None:
            return []
        signals = sorted(
            signal_store.list_active(),
            key=lambda s: s.net_profit_margin,
            reverse=True,
        )
        return [SignalResponse.from_model(s) for s in signals]

    @app.get("/signals/events", response_model=List[SignalEventResponse])
    def list_signal_events(
        limit: Optional[int] = Query(None, ge=1),
    ) -> List[SignalEventResponse]:
        """信号事件流（OPENED / UPDATED / CLOSED），按发生顺序返回。

        可选 ``limit`` 只返回最近的 N 个事件。信号存储未配置时返回空列表。
        """
        if signal_store is None:
            return []
        events = signal_store.list_events()
        if limit is not None:
            events = events[-limit:]
        return [SignalEventResponse.from_model(e) for e in events]

    @app.get("/signals/{group_id}", response_model=SignalResponse)
    def get_signal(group_id: str) -> SignalResponse:
        """单个活跃信号明细（Phase 3 · 切片 K）。信号存储未配置或不存在时 404。

        声明在 ``/signals/events`` 之后，避免静态路径被路径参数遮蔽。
        """
        if signal_store is None:
            raise HTTPException(status_code=404, detail="signal store not configured")
        signal = signal_store.get_active(group_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="signal not found")
        return SignalResponse.from_model(signal)

    @app.get("/metrics")
    def get_metrics() -> Dict[str, object]:
        """流水线运行指标快照（Phase Two · 切片 D）。

        包含周期数、信号开启/关闭累计、告警累计、最近一次周期的耗时与各项计数、
        以及当前活跃信号数。未配置 metrics 时返回空对象。
        """
        if metrics is None:
            return {}
        return metrics.snapshot()

    @app.get("/opportunities/{group_id}/history", include_in_schema=True)
    def opportunity_history(group_id: str, limit: int = Query(500, ge=1, le=5000)):
        """某机会组的净利润率历史时间序列（Phase 3 · 切片 K），供画价差/利润率走势。

        未配置历史存储时返回空列表。返回按时间升序的点（{value, at}）。
        """
        if history_store is None:
            return []
        points = history_store.opportunity_series(group_id, limit=limit)
        return [{"value": p.value, "at": p.at.isoformat(), "label": p.label} for p in points]

    @app.get("/markets/{platform}/{market_id}/history", include_in_schema=True)
    def market_history(platform: str, market_id: str, limit: int = Query(500, ge=1, le=5000)):
        """某市场 YES 价的历史时间序列（Phase 3 · 切片 K）。未配置历史存储时返回空列表。"""
        if history_store is None:
            return []
        points = history_store.market_series(platform, market_id, limit=limit)
        return [{"value": p.value, "at": p.at.isoformat(), "label": p.label} for p in points]

    # -----------------------------------------------------------------------
    # 交易 API（Phase 3 · 切片 H）—— 半自动确认流。
    #
    # ⚠️ 安全：这些端点会改变交易计划状态并（在确认时）触发执行。默认 dry-run
    # （由风控配置决定），不接入真实执行适配器时不会动真钱。生产对外开放前必须
    # 加鉴权（见迭代计划横向改进）。未配置 trading_service 时端点返回 404/空。
    # -----------------------------------------------------------------------

    @app.get("/trade/plans", response_model=List[TradePlanResponse])
    def list_trade_plans(
        status: Optional[str] = Query(None),
        _auth: None = Depends(_require_trade_auth),
    ):
        """列出交易计划（Phase 3 · 切片 H）。可选 ``status`` 过滤；按创建时间升序。

        未配置交易服务时返回空列表。
        """
        if trading_service is None:
            return []
        status_enum = None
        if status is not None:
            try:
                status_enum = TradePlanStatus(status)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"未知状态：{status}")
        plans = trading_service.list_plans(status=status_enum)
        return [TradePlanResponse.from_model(p) for p in plans]

    @app.get("/trade/plans/{plan_id}", response_model=TradePlanResponse)
    def get_trade_plan(plan_id: str, _auth: None = Depends(_require_trade_auth)):
        """单个交易计划明细（Phase 3 · 切片 H）。"""
        if trading_service is None:
            raise HTTPException(status_code=404, detail="trading service not configured")
        plan = trading_service.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        return TradePlanResponse.from_model(plan)

    @app.post("/trade/plans/{plan_id}/confirm", response_model=TradePlanResponse)
    async def confirm_trade_plan(plan_id: str, _auth: None = Depends(_require_trade_auth)):
        """人工确认一个待确认计划，随即按 dry-run 标志执行（Phase 3 · 切片 H）。"""
        if trading_service is None:
            raise HTTPException(status_code=404, detail="trading service not configured")
        try:
            plan = await trading_service.confirm(plan_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="plan not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return TradePlanResponse.from_model(plan)

    @app.post("/trade/plans/{plan_id}/reject", response_model=TradePlanResponse)
    def reject_trade_plan(plan_id: str, _auth: None = Depends(_require_trade_auth)):
        """人工拒绝一个待确认计划（Phase 3 · 切片 H）。"""
        if trading_service is None:
            raise HTTPException(status_code=404, detail="trading service not configured")
        try:
            plan = trading_service.reject(plan_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="plan not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return TradePlanResponse.from_model(plan)

    @app.get("/trade/balance")
    async def trade_balance(_auth: None = Depends(_require_trade_auth)):
        """各平台账户可用余额（USD）。当前为模拟盘余额，接入真实执行适配器后为真实余额。

        未配置交易服务时返回 `{}`。
        """
        if trading_service is None:
            return {}
        return await trading_service.get_balances()

    @app.get("/trade/positions")
    async def trade_positions(_auth: None = Depends(_require_trade_auth)):
        """各平台当前持仓列表。未配置交易服务时返回 `[]`。"""
        if trading_service is None:
            return []
        return await trading_service.get_positions()

    @app.get("/trade/exposure")
    def trade_exposure(_auth: None = Depends(_require_trade_auth)):
        """当前敞口快照（总敞口 + 各市场敞口），用于风控可视化。未配置时返回 `{}`。"""
        if trading_service is None:
            return {}
        return trading_service.exposure_snapshot()

    @app.get("/trade/pnl")
    def trade_pnl(_auth: None = Depends(_require_trade_auth)):
        """已执行计划的真实盈亏汇总（基于实际成交价审计「每笔是否真实有收益」）。

        统计累计已实现收益、盈利/亏损笔数并逐笔列出。dry-run 演练不计入。未配置返回 `{}`。
        """
        if trading_service is None:
            return {}
        return trading_service.pnl_summary()

    @app.get("/trade/onchain-balance")
    async def trade_onchain_balance(_auth: None = Depends(_require_trade_auth)):
        """各平台**真实链上**账户余额（只读，无需私钥；Phase 3 · 切片 I 第一步）。

        需配置 `scanner.polygon_rpc_url` 与平台 `options.wallet_address`。未配置时返回
        `{}`（仅有模拟盘余额）。这是接入真实下单前的连通验证：先确认能读到真实余额。
        """
        if not onchain_balances:
            return {}
        out: Dict[str, object] = {}
        for platform, cfg in onchain_balances.items():
            reader = cfg.get("reader")
            address = cfg.get("address")
            try:
                bal = await reader.get_balance(address)  # type: ignore[union-attr]
                out[platform] = {
                    "address": address,
                    "asset": cfg.get("asset", "USDC"),
                    "balance": bal,
                    "source": "onchain",
                }
            except Exception as exc:  # noqa: BLE001 - 单平台失败隔离
                out[platform] = {"address": address, "error": str(exc)}
        return out

    return app


__all__ = [
    "create_app",
    "MarketResponse",
    "OutcomeResponse",
    "GroupResponse",
    "OutcomeAlignmentResponse",
    "OpportunityResponse",
    "ArbLegResponse",
    "SignalResponse",
    "SignalEventResponse",
    "TradeLegResponse",
    "TradePlanResponse",
    "AdapterHealth",
    "HealthResponse",
    "HealthProvider",
]
