"""Unit tests for the ConfigLoader.

Covers:
- Adapter enable/disable resolution (Req 7.1, 7.3).
- Property 12: a disabled platform contributes zero markets (it is excluded
  from the resolved adapter set everywhere downstream).
- Default vs. user-overridden staleness threshold (Req 8.3, 8.4).
- Graceful handling of a missing Kalshi API key: only that adapter is disabled
  while others continue (Req 7.1 error handling).

**Validates: Requirements 7.1, 7.3, 8.3, 8.4**
"""

from __future__ import annotations

import textwrap

import pytest

from scanner.config import (
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    ScannerConfig,
    load_config,
    load_config_from_dict,
)
from scanner.fees import FlatFeeModel, KalshiFeeModel


# --- helpers ----------------------------------------------------------------

FULL_CONFIG = {
    "scanner": {
        "refresh_interval_seconds": 30,
        "fetch_timeout_seconds": 30,
        "staleness_threshold_seconds": 60,
        "match_confidence_min": 0.6,
    },
    "platforms": [
        {"name": "polymarket", "enabled": True, "fee_model": {"type": "flat", "rate": 0.0}},
        {
            "name": "kalshi",
            "enabled": True,
            "api_key_env": "KALSHI_API_KEY",
            "fee_model": {"type": "kalshi"},
        },
    ],
    "alerts": {"channels": ["log"], "criteria": {"min_net_profit_margin": 0.02}},
}


def write_yaml(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text))
    return str(path)


# --- adapter enable / disable (Req 7.1, 7.3, Property 12) --------------------

def test_enabled_platforms_loaded_by_name():
    cfg = load_config_from_dict(FULL_CONFIG)
    names = [p.name for p in cfg.enabled_platforms({"KALSHI_API_KEY": "secret"})]
    assert names == ["polymarket", "kalshi"]


def test_disabled_platform_is_excluded():
    data = {
        "platforms": [
            {"name": "polymarket", "enabled": True},
            {"name": "kalshi", "enabled": False, "api_key_env": "KALSHI_API_KEY"},
        ]
    }
    cfg = load_config_from_dict(data)
    names = [p.name for p in cfg.enabled_platforms({"KALSHI_API_KEY": "secret"})]
    # Property 12: kalshi is disabled, so it contributes nothing.
    assert names == ["polymarket"]


def test_disabled_platform_excluded_from_fee_models():
    data = {
        "platforms": [
            {"name": "polymarket", "enabled": True, "fee_model": {"type": "flat"}},
            {"name": "kalshi", "enabled": False, "fee_model": {"type": "kalshi"}},
        ]
    }
    cfg = load_config_from_dict(data)
    models = cfg.fee_models({})
    assert "kalshi" not in models
    assert "polymarket" in models


def test_platforms_default_to_enabled():
    cfg = load_config_from_dict({"platforms": [{"name": "polymarket"}]})
    assert [p.name for p in cfg.enabled_platforms({})] == ["polymarket"]


# --- missing API key disables only that adapter (Req 7.1) -------------------

def test_missing_api_key_disables_only_that_adapter():
    cfg = load_config_from_dict(FULL_CONFIG)
    # KALSHI_API_KEY is absent from this environment.
    names = [p.name for p in cfg.enabled_platforms({})]
    # Kalshi is dropped; polymarket continues (Req 7.1 error handling).
    assert names == ["polymarket"]


def test_empty_api_key_is_treated_as_missing():
    cfg = load_config_from_dict(FULL_CONFIG)
    names = [p.name for p in cfg.enabled_platforms({"KALSHI_API_KEY": ""})]
    assert names == ["polymarket"]


def test_present_api_key_enables_adapter():
    cfg = load_config_from_dict(FULL_CONFIG)
    names = [p.name for p in cfg.enabled_platforms({"KALSHI_API_KEY": "live-key"})]
    assert "kalshi" in names


def test_resolve_api_key_returns_value():
    cfg = load_config_from_dict(FULL_CONFIG)
    kalshi = next(p for p in cfg.platforms if p.name == "kalshi")
    assert kalshi.resolve_api_key({"KALSHI_API_KEY": "abc123"}) == "abc123"
    assert kalshi.resolve_api_key({}) is None


# --- default vs overridden staleness (Req 8.3, 8.4) -------------------------

def test_default_staleness_threshold_is_60s():
    # Req 8.3: no user value -> default 60s.
    cfg = load_config_from_dict({"platforms": []})
    assert cfg.scanner.staleness_threshold_seconds == DEFAULT_STALENESS_THRESHOLD_SECONDS
    assert cfg.scanner.staleness_threshold_seconds == 60.0


def test_user_overridden_staleness_threshold():
    # Req 8.4: user value replaces the default.
    cfg = load_config_from_dict({"scanner": {"staleness_threshold_seconds": 15}})
    assert cfg.scanner.staleness_threshold_seconds == 15.0


def test_other_scanner_settings_defaults():
    cfg = load_config_from_dict({})
    assert cfg.scanner.refresh_interval_seconds == 30.0
    assert cfg.scanner.fetch_timeout_seconds == 30.0
    assert cfg.scanner.match_confidence_min == 0.6


# --- signal store path (Phase Two · 切片 B) ----------------------------------

def test_signal_store_path_defaults_to_none():
    # 不配置时默认 None（表示用内存存储，重启丢失）。
    cfg = load_config_from_dict({})
    assert cfg.scanner.signal_store_path is None


def test_signal_store_path_overridden_by_yaml(tmp_path):
    # YAML 中配置的路径应覆盖默认值，启用 SQLite 持久化。
    path = write_yaml(
        tmp_path,
        """
        scanner:
          signal_store_path: signals.db
        """,
    )
    cfg = load_config(path)
    assert cfg.scanner.signal_store_path == "signals.db"


# --- fee model construction -------------------------------------------------

def test_fee_models_built_by_type():
    cfg = load_config_from_dict(FULL_CONFIG)
    models = cfg.fee_models({"KALSHI_API_KEY": "secret"})
    assert isinstance(models["polymarket"], FlatFeeModel)
    assert isinstance(models["kalshi"], KalshiFeeModel)


def test_flat_fee_model_uses_configured_rate():
    cfg = load_config_from_dict(
        {"platforms": [{"name": "p", "fee_model": {"type": "flat", "rate": 0.03}}]}
    )
    model = cfg.fee_models({})["p"]
    assert isinstance(model, FlatFeeModel)
    assert model.rate == 0.03


def test_kalshi_fee_model_custom_coefficient():
    cfg = load_config_from_dict(
        {"platforms": [{"name": "k", "fee_model": {"type": "kalshi", "coefficient": 0.035}}]}
    )
    model = cfg.fee_models({})["k"]
    assert isinstance(model, KalshiFeeModel)
    assert model.coefficient == 0.035


def test_unknown_fee_model_type_raises():
    cfg = load_config_from_dict({"platforms": [{"name": "x", "fee_model": {"type": "bogus"}}]})
    with pytest.raises(ValueError):
        cfg.fee_models({})


# --- alerts -----------------------------------------------------------------

def test_alert_config_loaded():
    cfg = load_config_from_dict(FULL_CONFIG)
    assert cfg.alerts.channels == ["log"]
    assert cfg.alerts.criteria.min_net_profit_margin == 0.02


def test_alert_config_defaults():
    cfg = load_config_from_dict({})
    assert cfg.alerts.channels == ["log"]
    assert cfg.alerts.criteria.min_net_profit_margin == 0.0


# --- YAML file loading (Req 7.1) --------------------------------------------

def test_load_config_from_yaml_file(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        scanner:
          staleness_threshold_seconds: 45
          match_confidence_min: 0.7
        platforms:
          - name: polymarket
            enabled: true
            fee_model: { type: flat, rate: 0.0 }
          - name: kalshi
            enabled: true
            api_key_env: KALSHI_API_KEY
            fee_model: { type: kalshi }
        alerts:
          channels: [log]
          criteria: { min_net_profit_margin: 0.02 }
        """,
    )
    cfg = load_config(path)
    assert isinstance(cfg, ScannerConfig)
    assert cfg.scanner.staleness_threshold_seconds == 45.0
    assert cfg.scanner.match_confidence_min == 0.7
    assert [p.name for p in cfg.platforms] == ["polymarket", "kalshi"]
    # With the key absent, only polymarket runs.
    assert [p.name for p in cfg.enabled_platforms({})] == ["polymarket"]


def test_load_empty_yaml_file_uses_defaults(tmp_path):
    path = write_yaml(tmp_path, "")
    cfg = load_config(path)
    assert cfg.scanner.staleness_threshold_seconds == 60.0
    assert cfg.platforms == []


def test_load_config_rejects_non_mapping_root(tmp_path):
    path = write_yaml(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_config(path)
