"""Configuration loading for the Prediction Market Arbitrage Scanner.

Loads YAML configuration into validated pydantic models and resolves which
platform adapters should actually run. The loader is the single place that
decides adapter membership, satisfying:

- Req 7.1: load the set of active platform adapters from configuration.
- Req 7.3 / Property 12: a disabled platform contributes zero markets to
  ingestion, matching, and detection.
- Req 8.3: a default staleness threshold of 60 seconds.
- Req 8.4: a user-specified staleness threshold replaces the default.

A platform that names an ``api_key_env`` whose environment variable is unset is
treated as disabled, but only that adapter -- every other enabled platform keeps
running (the Kalshi-key-missing case in the design's error-handling table).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Mapping, Optional

import yaml
from pydantic import BaseModel, Field

from scanner.fees import FeeModel, FlatFeeModel, KalshiFeeModel

logger = logging.getLogger(__name__)

# Req 8.3: the default staleness threshold when the user does not override it.
DEFAULT_STALENESS_THRESHOLD_SECONDS = 60.0
DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0
DEFAULT_MATCH_CONFIDENCE_MIN = 0.6


class FeeModelConfig(BaseModel):
    """Declarative selection of a platform fee model (design FeeModel section).

    ``type`` is ``"flat"`` or ``"kalshi"``. ``rate`` applies to the flat model;
    ``coefficient`` applies to the Kalshi model. Both are optional so a config
    can simply say ``{ type: kalshi }`` and accept the standard schedule.
    """

    type: str = "flat"
    rate: Optional[float] = None
    coefficient: Optional[float] = None

    def build(self) -> FeeModel:
        """Instantiate the concrete fee model selected by this config."""
        kind = self.type.lower()
        if kind == "flat":
            return FlatFeeModel(rate=self.rate if self.rate is not None else 0.0)
        if kind == "kalshi":
            if self.coefficient is not None:
                return KalshiFeeModel(coefficient=self.coefficient)
            return KalshiFeeModel()
        raise ValueError(f"unknown fee_model type: {self.type!r}")


class PlatformConfig(BaseModel):
    """Configuration for a single platform adapter (Req 7.1, 7.3)."""

    name: str
    enabled: bool = True
    api_key_env: Optional[str] = None
    fee_model: FeeModelConfig = Field(default_factory=FeeModelConfig)
    # 平台特定的额外选项（向后兼容、默认空）。例如 predict.fun 可用
    # ``options: {base_url: "https://api-testnet.predict.fun", max_markets: 150}``
    # 在不改代码的情况下切换测试网/生产、调整抓取规模。
    options: Dict[str, object] = Field(default_factory=dict)

    def resolve_api_key(self, env: Mapping[str, str]) -> Optional[str]:
        """Return the API key value from ``env``, or ``None`` if unset/empty."""
        if not self.api_key_env:
            return None
        value = env.get(self.api_key_env)
        if value is None or value == "":
            return None
        return value

    def is_available(self, env: Mapping[str, str]) -> bool:
        """True if this adapter should run.

        An adapter runs when it is enabled (Req 7.3) and, if it requires an API
        key (``api_key_env`` set), that environment variable is present. A
        missing key disables only this adapter (Req 7.1 error handling).
        """
        if not self.enabled:
            return False
        if self.api_key_env and self.resolve_api_key(env) is None:
            return False
        return True


class AlertCriteriaConfig(BaseModel):
    """User alert criteria (Req 6.2)."""

    min_net_profit_margin: float = 0.0
    min_match_confidence: Optional[float] = None
    platforms: Optional[List[str]] = None


class AlertConfig(BaseModel):
    """Alert delivery configuration (Req 6.2)."""

    channels: List[str] = Field(default_factory=lambda: ["log"])
    criteria: AlertCriteriaConfig = Field(default_factory=AlertCriteriaConfig)


class ScannerSettings(BaseModel):
    """Top-level scanner timing and threshold settings.

    Defaults satisfy Req 8.3 (60s staleness) and the design's ≤60s refresh
    interval. A user-provided value overrides the default (Req 8.4).
    """

    refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS
    fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS
    staleness_threshold_seconds: float = DEFAULT_STALENESS_THRESHOLD_SECONDS
    match_confidence_min: float = DEFAULT_MATCH_CONFIDENCE_MIN
    # 信号存储路径（Phase Two · 切片 B）。None 表示用内存存储（重启丢失）；
    # 设为文件路径（如 "signals.db"）则启用 SQLite 持久化，信号跨重启不丢。
    signal_store_path: Optional[str] = None
    # 数据接入韧性（Phase Two · 切片 C）。包装每个适配器，加入限流/退避/熔断。
    adapter_min_interval_seconds: float = 0.0   # 相邻平台请求的最小间隔（0=不限流）
    adapter_max_attempts: int = 3               # 单次操作对底层的最大尝试次数（含首次）
    adapter_backoff_base_seconds: float = 0.5   # 指数退避基数
    circuit_failure_threshold: int = 5          # 连续失败达此数则熔断
    circuit_reset_timeout_seconds: float = 30.0 # 熔断冷却时长，之后半开探测
    # 可观测性（Phase Two · 切片 D）：启用后日志以单行 JSON 输出，便于集中式采集。
    json_logging: bool = False
    # 信号质量（Phase Two · 切片 D）：匹配相似度后端。
    #   "lexical"     —— 纯词法（默认，最保守）。
    #   "normalizing" —— 词法 + 同义词/缩写归一，提升等价市场召回、减少漏信号。
    similarity_backend: str = "lexical"
    # 行情/机会历史持久化（Phase 3 · 切片 K）。None 表示不记录历史；设为文件路径
    # （如 "history.db"）则记录每周期的机会净利润率与市场价格时间序列，供趋势图/回看。
    history_store_path: Optional[str] = None
    history_retention_days: float = 7.0  # 历史保留窗口（天）
    # 市场价历史降采样（控制 history.db 膨胀）：采样最小间隔(秒) + 价格变化阈值。
    # 默认 60s + 0.005（0.5¢）：稳定市场几乎不写，库大小可控。
    history_sample_interval_seconds: float = 60.0
    history_min_price_delta: float = 0.005
    # 套利数据可靠性/可成交性闸门（真实数据驱动，防虚假套利）。
    #   max_implied_prob_divergence：同一事件两平台隐含 P(YES) 背离超过此值（绝对概率差，
    #     如 0.10=10个百分点）则跳过——大背离=定价脏/薄/结算口径不同，非真套利。None=不启用。
    #   min_recommended_size_usd：可成交规模低于此值则跳过（薄盘利润易被 gas 吃掉）。0=不启用。
    max_implied_prob_divergence: Optional[float] = None
    min_recommended_size_usd: float = 0.0
    # 链上 gas 成本核算（缺口 E-1）。gas 是每笔交易/每腿的固定成本，不随规模缩放。
    #   gas_cost_per_leg_usd：每条腿的链上交易成本估算（USD）。0=不建模（默认，行为不变）。
    #   min_net_profit_after_gas_usd：扣 gas 后绝对美元利润下限，低于则跳过（仅当 gas>0 生效）。
    gas_cost_per_leg_usd: float = 0.0
    min_net_profit_after_gas_usd: float = 0.0
    # 交易计划/订单持久化（Phase 3 · 切片 H）。None 表示用内存存储（重启丢失）；
    # 设为文件路径（如 "trades.db"）则启用 SQLite 持久化，计划/订单跨重启可恢复、可对账。
    trade_store_path: Optional[str] = None
    # 交易（写）API 鉴权（Phase 3 · 切片 H）。指定一个环境变量名，其值作为交易端点
    # 要求的 X-API-Key。None/未设 → 交易端点不鉴权（本机自用，与只读 API 一致）。
    # 真实下单前应配置。密钥只经环境变量注入，绝不写入配置文件/日志/响应。
    trade_api_key_env: Optional[str] = None
    # 链上只读余额查询（Phase 3 · 切片 I 第一步）。配置 Polygon JSON-RPC 端点后，
    # 结合平台 options 里的 wallet_address，可只读查询真实 USDC 余额（无需私钥）。
    # None/未设 → 不启用链上余额查询，仅显示模拟盘余额。
    polygon_rpc_url: Optional[str] = None


class RiskConfig(BaseModel):
    """风控阈值配置（Phase 3 · 切片 G）。

    下单前最后闸门 ``RiskManager`` 的阈值。默认值偏保守（小额、低敞口、要求人工确认、
    dry-run 开启），适合生产前验证；运营者按风险偏好放宽。映射到
    :class:`scanner.risk.RiskLimits`。
    """

    max_trade_size_usd: float = 100.0
    max_market_exposure_usd: float = 200.0
    max_total_exposure_usd: float = 1000.0
    max_slippage: float = 0.02
    min_net_profit_margin: float = 0.02
    max_data_age_seconds: float = 30.0
    bankroll_usd: float = 0.0
    max_bankroll_fraction: float = 0.25
    dry_run: bool = True
    require_confirmation: bool = True

    def build(self):
        """实例化 :class:`scanner.risk.RiskLimits`。"""
        from scanner.risk import RiskLimits

        return RiskLimits(
            max_trade_size_usd=self.max_trade_size_usd,
            max_market_exposure_usd=self.max_market_exposure_usd,
            max_total_exposure_usd=self.max_total_exposure_usd,
            max_slippage=self.max_slippage,
            min_net_profit_margin=self.min_net_profit_margin,
            max_data_age_seconds=self.max_data_age_seconds,
            bankroll_usd=self.bankroll_usd,
            max_bankroll_fraction=self.max_bankroll_fraction,
            dry_run=self.dry_run,
            require_confirmation=self.require_confirmation,
        )


class ScannerConfig(BaseModel):
    """The fully parsed scanner configuration."""

    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    platforms: List[PlatformConfig] = Field(default_factory=list)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    def enabled_platforms(
        self, env: Optional[Mapping[str, str]] = None
    ) -> List[PlatformConfig]:
        """Return only the platforms that should run (Req 7.1, 7.3, Property 12).

        Disabled platforms are excluded entirely. Platforms that require an API
        key whose environment variable is unset are also excluded, and a
        warning is logged for each so the operator knows why a platform was
        skipped. All other platforms continue.
        """
        if env is None:
            env = os.environ
        available: List[PlatformConfig] = []
        for platform in self.platforms:
            if not platform.enabled:
                logger.info("Platform %s is disabled in config; skipping.", platform.name)
                continue
            if platform.api_key_env and platform.resolve_api_key(env) is None:
                logger.warning(
                    "Platform %s requires environment variable %s which is not set; "
                    "disabling this adapter only.",
                    platform.name,
                    platform.api_key_env,
                )
                continue
            available.append(platform)
        return available

    def fee_models(
        self, env: Optional[Mapping[str, str]] = None
    ) -> Dict[str, FeeModel]:
        """Build fee models keyed by platform name for the enabled platforms."""
        return {p.name: p.fee_model.build() for p in self.enabled_platforms(env)}


def load_config_from_dict(data: Mapping[str, object]) -> ScannerConfig:
    """Validate an already-parsed config mapping into a ``ScannerConfig``."""
    return ScannerConfig.model_validate(dict(data or {}))


def load_config(path: str) -> ScannerConfig:
    """Load and validate scanner configuration from a YAML file (Req 7.1)."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("scanner config root must be a mapping")
    return load_config_from_dict(raw)


__all__ = [
    "DEFAULT_STALENESS_THRESHOLD_SECONDS",
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "DEFAULT_MATCH_CONFIDENCE_MIN",
    "FeeModelConfig",
    "PlatformConfig",
    "AlertCriteriaConfig",
    "AlertConfig",
    "RiskConfig",
    "ScannerSettings",
    "ScannerConfig",
    "load_config",
    "load_config_from_dict",
]
