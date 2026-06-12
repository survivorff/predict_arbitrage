"""End-to-end application wiring and entrypoint (Req 1.1, 6.2, 6.4, 6.5, 7.1, 7.4).

This module is the composition root for the Prediction Market Arbitrage Scanner.
It loads configuration, builds the enabled platform adapters by name, wires the
ingestion → matching → arbitrage → opportunity → alert pipeline, and exposes the
Read API as a ``uvicorn``-mountable FastAPI app.

Pipeline (run after every ingestion cycle, design "Pipeline timing"):

    re-rank (optional) → re-match → re-evaluate arbitrage → reconcile listing → emit alerts

The pipeline is exposed as :meth:`ScannerApplication.run_pipeline_once` (and
:meth:`ScannerApplication.ingest_once_and_run_pipeline`) so it can be driven
deterministically in tests without real timers, while :meth:`ScannerApplication.run`
launches the live ingestion loop plus a periodic pipeline loop alongside uvicorn.

Adapter membership is config-driven (Req 7.1, 7.3, Property 12): only platforms
that ``ScannerConfig.enabled_platforms`` returns are built, so a disabled
platform — or one whose required API-key env var is unset (e.g. Kalshi) — is
excluded from ingestion, matching, and detection.

.. note::
   SECURITY: the Read API mounted here is **unauthenticated**. It is a read-only
   interface over public/aggregated market data (Req 7.4) and intentionally has
   no auth or access control in Phase One. Do not expose it directly to an
   untrusted network without placing an authenticating reverse proxy / gateway
   in front of it, and never let it surface secrets (API keys live only in the
   adapters via environment variables, never in API responses).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from fastapi import FastAPI

from scanner.adapters.base import PlatformAdapter
from scanner.adapters.kalshi import KalshiAdapter
from scanner.adapters.polymarket import PolymarketAdapter
from scanner.adapters.predictfun import PredictFunAdapter
from scanner.alerts import (
    Alert,
    AlertChannel,
    AlertCriteria,
    AlertService,
    LarkChannel,
    LogChannel,
    TelegramChannel,
    WebhookChannel,
    _default_sleep,
)
from scanner.api import AdapterHealth as AdapterHealthModel
from scanner.api import create_app
from scanner.arbitrage import ArbitrageEngine
from scanner.config import (
    PlatformConfig,
    ScannerConfig,
    load_config,
)
from scanner.fees import FlatFeeModel, KalshiFeeModel
from scanner.ingestion import IngestionService
from scanner.history import NullHistoryStore, SqliteHistoryStore
from scanner.execution import PaperExecutionAdapter
from scanner.execution_engine import ExecutionEngine
from scanner.onchain import ErcBalanceReader
from scanner.risk import RiskManager
from scanner.trade_store import InMemoryTradeStore, SqliteTradeStore
from scanner.trading import TradingService
from scanner.matching import MatchingEngine, NormalizingLexicalSimilarity
from scanner.models import ArbitrageOpportunity, SignalEventType
from scanner.observability import PipelineMetrics, configure_json_logging
from scanner.opportunities import OpportunityService
from scanner.ranking import RankingService
from scanner.resilience import CircuitState, ResilientAdapter
from scanner.signals import (
    InMemorySignalStore,
    SignalService,
    SqliteSignalStore,
    _utc_now,
)
from scanner.store import InMemoryMarketStore, MarketStore, OpportunityStore

logger = logging.getLogger(__name__)

# Where the default config is loaded from for the uvicorn entrypoint. Overridable
# via the SCANNER_CONFIG environment variable.
DEFAULT_CONFIG_PATH = os.environ.get("SCANNER_CONFIG", "config.yaml")

# Factory signature for building a platform adapter from its config + env.
AdapterFactory = Callable[[PlatformConfig, Mapping[str, str]], PlatformAdapter]


def _build_polymarket(platform: PlatformConfig, env: Mapping[str, str]) -> PlatformAdapter:
    """Build a PolymarketAdapter, honoring a configured flat fee model.

    平台特定选项经 ``platform.options`` 传入：``page_size``（Gamma 翻页大小）、
    ``max_markets``（单次发现上限，按成交量降序优先）。
    """
    fee_model = platform.fee_model.build()
    opts = platform.options or {}
    kwargs: Dict[str, object] = {}
    if isinstance(fee_model, FlatFeeModel):
        kwargs["fee_model"] = fee_model
    if "page_size" in opts:
        kwargs["page_size"] = int(opts["page_size"])
    if "max_markets" in opts:
        kwargs["max_markets"] = int(opts["max_markets"])
    if "categories" in opts and isinstance(opts["categories"], list):
        kwargs["categories"] = [str(c) for c in opts["categories"]]
    return PolymarketAdapter(**kwargs)


def _build_kalshi(platform: PlatformConfig, env: Mapping[str, str]) -> PlatformAdapter:
    """Build a KalshiAdapter with its API key (from env) and fee model."""
    fee_model = platform.fee_model.build()
    kalshi_fee = fee_model if isinstance(fee_model, KalshiFeeModel) else KalshiFeeModel()
    return KalshiAdapter(
        api_key=platform.resolve_api_key(env),
        fee_model=kalshi_fee,
    )


def _build_predictfun(platform: PlatformConfig, env: Mapping[str, str]) -> PlatformAdapter:
    """Build a PredictFunAdapter，可选携带 x-api-key（从 env，经 api_key_env 解析）。

    平台特定选项经 ``platform.options`` 传入：``base_url``（切换测试网/生产）、
    ``max_markets``（单次发现上限）、``max_concurrency``（订单簿并发数）。
    """
    fee_model = platform.fee_model.build()
    flat = fee_model if isinstance(fee_model, FlatFeeModel) else FlatFeeModel(0.0)
    opts = platform.options or {}
    kwargs: Dict[str, object] = {
        "fee_model": flat,
        "api_key": platform.resolve_api_key(env),
    }
    if "base_url" in opts:
        kwargs["base_url"] = str(opts["base_url"])
    if "max_markets" in opts:
        kwargs["max_markets"] = int(opts["max_markets"])
    if "max_concurrency" in opts:
        kwargs["max_concurrency"] = int(opts["max_concurrency"])
    return PredictFunAdapter(**kwargs)


# Maps a platform name (Req 7.1) to the factory that builds its adapter. New
# platforms are added here without touching the ingestion/matching/arb core
# (Req 7.2).
ADAPTER_FACTORIES: Dict[str, AdapterFactory] = {
    "polymarket": _build_polymarket,
    "kalshi": _build_kalshi,
    "predictfun": _build_predictfun,
}


def build_adapters(
    config: ScannerConfig,
    env: Optional[Mapping[str, str]] = None,
) -> List[PlatformAdapter]:
    """Build adapters for every enabled, available platform (Req 7.1, 7.3).

    Only platforms returned by :meth:`ScannerConfig.enabled_platforms` are
    built, so disabled platforms and platforms missing a required API key are
    excluded entirely (Property 12). Unknown platform names are logged and
    skipped rather than crashing startup.
    """
    if env is None:
        env = os.environ
    adapters: List[PlatformAdapter] = []
    for platform in config.enabled_platforms(env):
        factory = ADAPTER_FACTORIES.get(platform.name.lower())
        if factory is None:
            logger.warning(
                "No adapter factory registered for platform %r; skipping.",
                platform.name,
            )
            continue
        adapters.append(factory(platform, env))
    return adapters


def build_alert_channels(channel_names: Sequence[str]) -> List[AlertChannel]:
    """Build alert channels from their configured names (Req 6.2).

    ``log`` builds a :class:`LogChannel`. ``webhook`` builds a
    :class:`WebhookChannel` when a ``SCANNER_WEBHOOK_URL`` env var is set,
    otherwise it is skipped with a warning (its URL is not part of the static
    config schema in Phase One).
    """
    channels: List[AlertChannel] = []
    for name in channel_names:
        key = name.strip().lower()
        if key == "log":
            channels.append(LogChannel())
        elif key == "webhook":
            url = os.environ.get("SCANNER_WEBHOOK_URL")
            if url:
                channels.append(WebhookChannel(url=url))
            else:
                logger.warning(
                    "Webhook alert channel configured but SCANNER_WEBHOOK_URL "
                    "is unset; skipping webhook channel."
                )
        elif key == "telegram":
            # 凭证只经环境变量注入，绝不入配置/日志。缺失则优雅降级（跳过该通道）。
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if token and chat_id:
                channels.append(TelegramChannel(token=token, chat_id=chat_id))
            else:
                logger.warning(
                    "Telegram alert channel configured but TELEGRAM_BOT_TOKEN / "
                    "TELEGRAM_CHAT_ID is unset; skipping telegram channel."
                )
        elif key == "lark":
            webhook = os.environ.get("LARK_WEBHOOK_URL")
            if webhook:
                channels.append(LarkChannel(webhook_url=webhook))
            else:
                logger.warning(
                    "Lark alert channel configured but LARK_WEBHOOK_URL is unset; "
                    "skipping lark channel."
                )
        else:
            logger.warning("Unknown alert channel %r; skipping.", name)
    return channels


def _criteria_from_config(config: ScannerConfig) -> AlertCriteria:
    """Convert the config's alert criteria into the AlertService's criteria."""
    cfg = config.alerts.criteria
    return AlertCriteria(
        min_net_profit_margin=cfg.min_net_profit_margin,
        min_match_confidence=cfg.min_match_confidence,
        platforms=cfg.platforms,
    )


class ScannerApplication:
    """Composition root tying every component into a runnable pipeline.

    Build instances with :func:`build_application` rather than constructing this
    directly; the constructor simply stores the fully wired collaborators.
    """

    def __init__(
        self,
        *,
        config: ScannerConfig,
        adapters: Sequence[PlatformAdapter],
        market_store: MarketStore,
        opportunity_store: OpportunityStore,
        ingestion_service: IngestionService,
        ranking_service: RankingService,
        matching_engine: MatchingEngine,
        arbitrage_engine: ArbitrageEngine,
        opportunity_service: OpportunityService,
        alert_service: AlertService,
        signal_service: SignalService,
        app: FastAPI,
        metrics: Optional[PipelineMetrics] = None,
        history_store=None,
        trading_service=None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config
        self.adapters = list(adapters)
        self.market_store = market_store
        self.opportunity_store = opportunity_store
        self.ingestion_service = ingestion_service
        self.ranking_service = ranking_service
        self.matching_engine = matching_engine
        self.arbitrage_engine = arbitrage_engine
        self.opportunity_service = opportunity_service
        self.alert_service = alert_service
        self.signal_service = signal_service
        self.app = app
        self._clock = clock or _utc_now
        self.metrics = metrics if metrics is not None else PipelineMetrics(clock=self._clock)
        self.history_store = history_store if history_store is not None else NullHistoryStore()
        self.trading_service = trading_service
        self._background_tasks: List[asyncio.Task] = []

    # -- the downstream pipeline ------------------------------------------- #
    async def run_pipeline_once(self) -> List[Alert]:
        """Run one downstream pipeline pass over the current market snapshot.

        Steps (design "Pipeline timing"):

        1. **re-rank** (optional) — recompute the ranked market view. Ranking is
           consumed by the API on demand, so this is a no-op side-effect-wise but
           kept as an explicit, cheap step so the pipeline mirrors the design.
        2. **re-match** — group equivalent cross-platform markets.
        3. **re-evaluate** — compute arbitrage opportunities for eligible groups
           (the engine already skips stale and sub-confidence-threshold groups).
        4. **reconcile** — filter/sort into the opportunity listing, dropping
           non-positive and below-threshold margins (Req 6.4, Property 10).
        5. **emit alerts** — deliver alerts for newly listable opportunities,
           with retry on failing channels and dedupe until they clear (Req 6.2,
           6.5, Property 11).

        Returns the alerts that fired this pass.
        """
        markets = self.market_store.list_all()
        cycle_start = self._clock()

        # 1. Re-rank (optional). Ranking is applied at query time by the API;
        #    invoking it here keeps the pipeline faithful to the design and lets
        #    the ranked view be logged/observed without storing derived state.
        self.ranking_service.rank(markets, by="volume")

        # 2. Re-match.
        groups = self.matching_engine.match(markets)

        # 2b. 实时性（F-2）：把当前匹配组里的市场 ID 反馈给适配器「固定抓取」名单，
        #     使已建立的跨平台套利对不因成交量下滑跌出 top-N 而忽隐忽现。
        self._update_pinned_markets(groups)

        # 3. Re-evaluate arbitrage (stale / low-confidence groups are skipped).
        opportunities = self.arbitrage_engine.evaluate(groups)

        # 4. Reconcile into the user-facing listing (Req 6.4, Property 10).
        listed = self.opportunity_service.reconcile(opportunities)

        # 4b. 信号生命周期对账（Phase Two · 切片 A）：用可列出的机会快照驱动
        #     信号 OPEN → SUSTAINED → CLOSED 状态转移并产出事件流。
        signal_events = self.signal_service.reconcile(listed)

        # 4b-2. 半自动交易提议（Phase 3 · 切片 H）：对可交易机会跑风控，为通过的
        #       生成待确认计划（去重）。绝不自动下单——需人工经交易 API 确认。
        #       默认 dry-run（风控配置），执行也只演练不动真钱。
        if self.trading_service is not None:
            try:
                self.trading_service.propose(listed)
            except Exception:  # noqa: BLE001 - 交易提议失败不应中断主流水线
                logger.exception("交易提议失败；继续。")

        # 4c. 历史时间序列持久化（Phase 3 · 切片 K）：记录本周期机会净利润率与市场价，
        #     供仪表盘画价差/利润率走势、信号回看。NullHistoryStore 时为空操作。
        try:
            self.history_store.record_opportunities(listed)
            self.history_store.record_markets(markets)
        except Exception:  # noqa: BLE001 - 历史写入失败不应中断主流水线
            logger.exception("历史持久化写入失败；继续。")

        # 5. Emit alerts for newly listable opportunities (Req 6.2, 6.5).
        #    Passing the reconciled listing (positive-margin only) means an
        #    opportunity that converges away disappears from the input, which
        #    clears its alerted state so it can re-alert if it reappears
        #    (Property 11 dedupe-until-cleared semantics).
        fired = await self.alert_service.on_new_opportunities(listed)

        # 6. 可观测性埋点（Phase Two · 切片 D）：记录本周期的耗时与计数。
        opened = sum(
            1 for e in signal_events if e.event_type is SignalEventType.OPENED
        )
        closed = sum(
            1 for e in signal_events if e.event_type is SignalEventType.CLOSED
        )
        duration = (self._clock() - cycle_start).total_seconds()
        self.metrics.record_cycle(
            duration_seconds=duration,
            markets_count=len(markets),
            groups_count=len(groups),
            opportunities_count=len(listed),
            active_signals_count=len(self.signal_service.store.list_active()),
            signals_opened=opened,
            signals_closed=closed,
            alerts_fired=len(fired),
        )
        return fired

    def _update_pinned_markets(self, groups) -> None:
        """把匹配组里各平台的成员 market_id 反馈给支持「固定抓取」的适配器（F-2）。

        当前 predict.fun 适配器有 ``pinned_ids``——这些市场即使跌出成交量 top-N 也会
        被单独按 ID 抓取，避免已建立的套利对忽隐忽现。适配器可能被 ResilientAdapter
        包装，故逐层取底层适配器。失败静默（实时性优化，不应影响主流程）。
        """
        try:
            by_platform: Dict[str, set] = {}
            for g in groups:
                for m in g.members:
                    by_platform.setdefault(m.platform, set()).add(m.market_id)
            for adapter in self.adapters:
                base = getattr(adapter, "_adapter", adapter)  # 解包 ResilientAdapter
                if hasattr(base, "pinned_ids"):
                    base.pinned_ids = by_platform.get(base.name, set())
        except Exception:  # noqa: BLE001 - 实时性优化失败不影响主流水线
            logger.debug("更新 pinned 市场失败；忽略。", exc_info=True)

    async def ingest_once_and_run_pipeline(self) -> List[Alert]:
        """Run one ingestion cycle for every adapter, then the pipeline.

        This is the deterministic driver used by tests: it ingests a single
        cycle from each adapter (failure-isolated by the IngestionService) and
        then runs the downstream pipeline exactly once.
        """
        for adapter in self.adapters:
            await self.ingestion_service._cycle(adapter)
        return await self.run_pipeline_once()

    # -- live loops alongside uvicorn -------------------------------------- #
    async def _pipeline_loop(self) -> None:
        """Periodically run the downstream pipeline on the refresh interval.

        运行一次「预热」后立即跑一轮，避免启动后 30 秒内仪表盘空白（首轮行情已摄取
        即可计算匹配/套利）；之后按 refresh_interval 周期运行。
        """
        interval = self.ingestion_service.refresh_interval
        # 预热：给首轮摄取留一点时间，然后立即跑一轮，避免长时间空白。
        await asyncio.sleep(min(5.0, interval))
        while True:
            try:
                await self.run_pipeline_once()
            except Exception:  # noqa: BLE001 - never let a bad cycle kill the loop
                logger.exception("Pipeline cycle failed; continuing.")
            await asyncio.sleep(interval)

    async def start_background(self) -> None:
        """Launch the ingestion loop and the periodic pipeline loop.

        Each adapter ingests on its own loop (IngestionService.run), and a
        separate loop runs the downstream pipeline on the same interval. Both
        run until :meth:`stop_background` cancels them.
        """
        if self._background_tasks:
            return
        self._background_tasks = [
            asyncio.ensure_future(self.ingestion_service.run()),
            asyncio.ensure_future(self._pipeline_loop()),
        ]

    async def stop_background(self) -> None:
        """Cancel the background ingestion/pipeline loops and await teardown."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks = []
        # 关闭持有 SQLite 连接的存储，避免连接泄漏（连接以 check_same_thread=False
        # 长持有）。各 store 的 close 若不存在则跳过。
        for store in (
            getattr(self.signal_service, "store", None),
            self.history_store,
            getattr(self.trading_service, "store", None),
        ):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭失败不应阻断停机
                    logger.exception("关闭存储连接失败；继续停机。")


def build_application(
    config: ScannerConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    adapters: Optional[Sequence[PlatformAdapter]] = None,
    alert_channels: Optional[Sequence[AlertChannel]] = None,
    clock: Optional[Callable[[], datetime]] = None,
    alert_sleep: Optional[Callable[[float], "asyncio.Future"]] = None,
    alert_backoff_base: Optional[float] = None,
) -> ScannerApplication:
    """Wire a :class:`ScannerApplication` from configuration (Req 7.1, 7.4).

    Args:
        config: The parsed scanner configuration.
        env: Environment mapping for adapter API-key resolution (defaults to
            ``os.environ``).
        adapters: Optional explicit adapters, bypassing config-driven
            construction. Used by tests to inject fakes; production passes
            ``None`` so adapters are built from config (Req 7.1, 7.3).
        alert_channels: Optional explicit alert channels, bypassing the
            config-name-driven construction. Tests inject channels (including a
            failing one) here.
        clock: Optional shared clock injected into the IngestionService and
            ArbitrageEngine so timestamps and staleness are deterministic.
        alert_sleep: Optional async sleep used for alert retry backoff; inject a
            no-op in tests to avoid real delays.
        alert_backoff_base: Optional backoff base seconds for alert retries.

    Returns:
        A fully wired :class:`ScannerApplication`.
    """
    if env is None:
        env = os.environ

    settings = config.scanner

    # 可观测性（Phase Two · 切片 D）：按配置启用结构化 JSON 日志。
    if settings.json_logging:
        configure_json_logging()

    market_store = InMemoryMarketStore()
    opportunity_store = OpportunityStore()

    resolved_adapters: List[PlatformAdapter] = (
        list(adapters) if adapters is not None else build_adapters(config, env)
    )

    # 数据接入韧性（Phase Two · 切片 C）：从配置构造的适配器统一用
    # ResilientAdapter 包装，加入限流/退避/熔断；显式注入的适配器（如测试中的
    # FakeAdapter）保持原样，由调用方自行决定是否包装。
    if adapters is None and settings.adapter_min_interval_seconds is not None:
        resolved_adapters = [
            ResilientAdapter(
                a,
                min_interval=settings.adapter_min_interval_seconds,
                max_attempts=settings.adapter_max_attempts,
                backoff_base=settings.adapter_backoff_base_seconds,
                failure_threshold=settings.circuit_failure_threshold,
                reset_timeout=settings.circuit_reset_timeout_seconds,
                clock=clock,
            )
            for a in resolved_adapters
        ]

    ingestion_service = IngestionService(
        resolved_adapters,
        market_store,
        refresh_interval=settings.refresh_interval_seconds,
        fetch_timeout=settings.fetch_timeout_seconds,
        staleness_threshold=settings.staleness_threshold_seconds,
        clock=clock,
    )

    ranking_service = RankingService()
    # 信号质量（Phase Two · 切片 D）：按配置选择匹配相似度后端。
    # match_confidence_min 同时作为匹配引擎的 score_threshold —— 否则匹配引擎会用
    # 默认 0.5 放行低置信度组（如 "夺冠" 被错配到 "赢小组赛"，相似度 0.73），即使
    # 套利引擎的 confidence_threshold 设为 0.6 也拦不住，导致虚假套利。一处配置、两处生效。
    if settings.similarity_backend == "normalizing":
        matching_engine = MatchingEngine(
            similarity=NormalizingLexicalSimilarity(),
            score_threshold=settings.match_confidence_min,
        )
    else:
        matching_engine = MatchingEngine(
            score_threshold=settings.match_confidence_min,
        )

    arbitrage_engine = ArbitrageEngine(
        fee_models=config.fee_models(env),
        confidence_threshold=settings.match_confidence_min,
        clock=clock if clock is not None else ArbitrageEngine.clock,
        staleness_threshold_seconds=settings.staleness_threshold_seconds,
        max_implied_prob_divergence=settings.max_implied_prob_divergence,
        min_recommended_size_usd=settings.min_recommended_size_usd,
        gas_cost_per_leg_usd=settings.gas_cost_per_leg_usd,
        min_net_profit_after_gas_usd=settings.min_net_profit_after_gas_usd,
    )

    opportunity_service = OpportunityService(
        store=opportunity_store,
        min_net_profit_margin=config.alerts.criteria.min_net_profit_margin,
    )

    channels: List[AlertChannel] = (
        list(alert_channels)
        if alert_channels is not None
        else build_alert_channels(config.alerts.channels)
    )
    alert_service = AlertService(
        channels=channels,
        criteria=_criteria_from_config(config),
        sleep=alert_sleep if alert_sleep is not None else _default_sleep,
        backoff_base=alert_backoff_base if alert_backoff_base is not None else 0.5,
    )

    signal_store = (
        SqliteSignalStore(settings.signal_store_path)
        if settings.signal_store_path
        else InMemorySignalStore()
    )
    signal_service = SignalService(
        store=signal_store,
        clock=clock if clock is not None else _utc_now,
    )

    # 行情/机会历史持久化（Phase 3 · 切片 K）：按配置启用。
    history_store = (
        SqliteHistoryStore(
            settings.history_store_path,
            retention_days=settings.history_retention_days,
            sample_interval_seconds=settings.history_sample_interval_seconds,
            min_price_delta=settings.history_min_price_delta,
            clock=clock if clock is not None else _utc_now,
        )
        if settings.history_store_path
        else NullHistoryStore()
    )

    metrics = PipelineMetrics(clock=clock if clock is not None else _utc_now)

    # 半自动交易编排（Phase 3 · 切片 H）：风控闸门 + dry-run 执行 + 待确认计划。
    # ⚠️ 安全：默认 dry-run（风控配置 risk.dry_run），且仅接入模拟盘执行适配器
    # （PaperExecutionAdapter，不动真钱）。真实下单需接入真实执行适配器（切片 I）
    # 并显式关闭 dry-run。交易计划经 TradeStore 持久化（复用信号存储路径风格）。
    risk_manager = RiskManager(limits=config.risk.build())
    execution_adapters = {
        p.name: PaperExecutionAdapter(name=p.name)
        for p in config.enabled_platforms(env)
    }
    execution_engine = ExecutionEngine(
        adapters=execution_adapters,
        clock=clock if clock is not None else _utc_now,
    )
    trade_store = (
        SqliteTradeStore(settings.trade_store_path)
        if settings.trade_store_path
        else InMemoryTradeStore()
    )
    trading_service = TradingService(
        risk_manager=risk_manager,
        execution_engine=execution_engine,
        store=trade_store,
        clock=clock if clock is not None else _utc_now,
    )

    # 链上只读余额查询（Phase 3 · 切片 I 第一步）：配置 Polygon RPC + 平台 wallet_address
    # 后，可只读查询真实 USDC 余额（无需私钥）。未配置则为空，仅显示模拟盘余额。
    onchain_balances: Dict[str, Dict[str, object]] = {}
    if settings.polygon_rpc_url:
        for p in config.enabled_platforms(env):
            addr = (p.options or {}).get("wallet_address")
            if p.name == "polymarket" and addr:
                onchain_balances[p.name] = {
                    "reader": ErcBalanceReader(rpc_url=settings.polygon_rpc_url),
                    "address": str(addr),
                    "asset": "USDC",
                }

    # 健康视图（Phase Two · 切片 C）：合并「市场快照健康」与「韧性指标（熔断状态/
    # 成功失败计数）」。仅当适配器被 ResilientAdapter 包装时才有韧性指标。
    def _health_provider() -> List[AdapterHealthModel]:
        by_platform: Dict[str, List] = {}
        for market in market_store.list_all():
            by_platform.setdefault(market.platform, []).append(market)
        metrics_by_name = {
            a.name: a.metrics
            for a in resolved_adapters
            if isinstance(a, ResilientAdapter)
        }
        names = sorted(set(by_platform) | set(metrics_by_name))
        result: List[AdapterHealthModel] = []
        for name in names:
            markets = by_platform.get(name, [])
            last_cycle = max((m.retrieved_at for m in markets), default=None)
            healthy = any(not m.is_stale for m in markets) if markets else False
            m = metrics_by_name.get(name)
            if m is not None:
                # 熔断打开视为不健康。
                healthy = healthy and m.circuit_state != CircuitState.OPEN.value
                if m.last_success_at is not None and last_cycle is None:
                    last_cycle = m.last_success_at
            result.append(
                AdapterHealthModel(
                    name=name,
                    healthy=healthy,
                    market_count=len(markets),
                    last_successful_cycle=last_cycle,
                    last_error=m.last_error if m else None,
                    circuit_state=m.circuit_state if m else None,
                    success_count=m.success_count if m else None,
                    failure_count=m.failure_count if m else None,
                )
            )
        return result

    app = create_app(
        market_store=market_store,
        opportunity_service=opportunity_service,
        opportunity_store=opportunity_store,
        ranking_service=ranking_service,
        matching_engine=matching_engine,
        alert_config=config.alerts,
        signal_store=signal_store,
        health_provider=_health_provider,
        metrics=metrics,
        history_store=history_store,
        trading_service=trading_service,
        trade_api_key=(
            env.get(settings.trade_api_key_env) if settings.trade_api_key_env else None
        ),
        onchain_balances=onchain_balances or None,
        staleness_threshold=settings.staleness_threshold_seconds,
        sizing_config={
            "bankroll_usd": config.risk.bankroll_usd,
            "max_bankroll_fraction": config.risk.max_bankroll_fraction,
        },
    )

    application = ScannerApplication(
        config=config,
        adapters=resolved_adapters,
        market_store=market_store,
        opportunity_store=opportunity_store,
        ingestion_service=ingestion_service,
        ranking_service=ranking_service,
        matching_engine=matching_engine,
        arbitrage_engine=arbitrage_engine,
        opportunity_service=opportunity_service,
        alert_service=alert_service,
        signal_service=signal_service,
        app=app,
        metrics=metrics,
        history_store=history_store,
        trading_service=trading_service,
        clock=clock,
    )

    # Launch the ingestion + pipeline loops alongside uvicorn via FastAPI's
    # lifespan so the live service starts scanning on startup and tears the
    # background loops down on shutdown. Assigned onto the app created by
    # create_app without disturbing its route registration.
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):  # pragma: no cover - exercised under uvicorn
        await application.start_background()
        try:
            yield
        finally:
            await application.stop_background()

    app.router.lifespan_context = _lifespan

    return application


def _load_default_config() -> ScannerConfig:
    """Load the config at ``DEFAULT_CONFIG_PATH`` or fall back to defaults."""
    try:
        return load_config(DEFAULT_CONFIG_PATH)
    except FileNotFoundError:
        logger.warning(
            "Config file %s not found; starting with default configuration "
            "(no platforms enabled).",
            DEFAULT_CONFIG_PATH,
        )
        return ScannerConfig()


def create_default_app() -> FastAPI:
    """Build the module-level FastAPI app for ``uvicorn scanner.app:app``."""
    application = build_application(_load_default_config())
    return application.app


# Module-level ASGI app for ``uvicorn scanner.app:app``. Built from the default
# config path; missing config degrades gracefully to an empty configuration.
app: FastAPI = create_default_app()


def main() -> None:
    """Console entrypoint: serve the Read API and run the scanner via uvicorn.

    The ingestion loop and periodic pipeline are started by the FastAPI startup
    event registered in :func:`build_application`, so running uvicorn against
    this module's ``app`` boots the full scanner.
    """
    import uvicorn  # imported lazily so importing this module stays cheap

    host = os.environ.get("SCANNER_HOST", "127.0.0.1")
    port = int(os.environ.get("SCANNER_PORT", "8000"))
    logger.warning(
        "Starting Prediction Market Arbitrage Scanner read API on %s:%s. "
        "NOTE: this API is unauthenticated; place it behind an authenticating "
        "gateway before exposing it to untrusted networks.",
        host,
        port,
    )
    uvicorn.run(app, host=host, port=port)


__all__ = [
    "ScannerApplication",
    "build_application",
    "build_adapters",
    "build_alert_channels",
    "ADAPTER_FACTORIES",
    "create_default_app",
    "app",
    "main",
    "DEFAULT_CONFIG_PATH",
]


if __name__ == "__main__":  # pragma: no cover
    main()
