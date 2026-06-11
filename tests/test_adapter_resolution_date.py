"""适配器结算日期解析测试(Tier-0 红线 C 数据来源)。

- Polymarket:从 Gamma 的 ``endDate``(含时间)/``endDateIso``(仅日期)解析。
- predict.fun:markets 端点无结构化日期字段,从 ``description`` 自由文本解析
  「Month Day, Year」截止日(优先 by/before 之后的日期)。

这些是 ``CanonicalMarket.resolution_date`` 的数据来源,匹配引擎据此做日期硬 veto。
"""

from __future__ import annotations

from datetime import datetime, timezone

from scanner.adapters.polymarket import PolymarketAdapter
from scanner.adapters.predictfun import PredictFunAdapter


# --------------------------------------------------------------------------- #
# Polymarket: endDate / endDateIso
# --------------------------------------------------------------------------- #
def test_polymarket_parse_end_date_with_time():
    dt = PolymarketAdapter._parse_end_date({"endDate": "2026-12-31T00:00:00Z"})
    assert dt == datetime(2026, 12, 31, tzinfo=timezone.utc)


def test_polymarket_parse_end_date_iso_fallback():
    dt = PolymarketAdapter._parse_end_date({"endDateIso": "2026-04-30"})
    assert dt == datetime(2026, 4, 30, tzinfo=timezone.utc)


def test_polymarket_prefers_endDate_over_iso():
    dt = PolymarketAdapter._parse_end_date(
        {"endDate": "2026-06-30T12:00:00Z", "endDateIso": "2026-06-30"}
    )
    assert dt == datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def test_polymarket_parse_end_date_missing():
    assert PolymarketAdapter._parse_end_date({}) is None
    assert PolymarketAdapter._parse_end_date({"endDate": ""}) is None
    assert PolymarketAdapter._parse_end_date({"endDate": "not-a-date"}) is None


# --------------------------------------------------------------------------- #
# predict.fun: 从描述文本解析
# --------------------------------------------------------------------------- #
def test_predictfun_parse_resolution_date_from_description():
    adapter = PredictFunAdapter()
    desc = (
        'This market will resolve to "Yes" if any US federal agency definitively '
        "states that extraterrestrial life or technology exists by December 31, "
        "2026, 11:59 PM ET. Otherwise, it resolves No."
    )
    assert adapter._parse_resolution_date(desc) == datetime(
        2026, 12, 31, tzinfo=timezone.utc
    )


def test_predictfun_prefers_by_before_date():
    adapter = PredictFunAdapter()
    # 文本含两个日期:开始日(January 1, 2026)与截止日(before April 30, 2026)。
    # 应优先取 before 之后的截止日。
    desc = (
        "Trading opened January 1, 2026. This market resolves Yes if the event "
        "occurs before April 30, 2026. Otherwise No."
    )
    assert adapter._parse_resolution_date(desc) == datetime(
        2026, 4, 30, tzinfo=timezone.utc
    )


def test_predictfun_abbreviated_month():
    adapter = PredictFunAdapter()
    assert adapter._parse_resolution_date("Resolves by Sep 30, 2026.") == datetime(
        2026, 9, 30, tzinfo=timezone.utc
    )


def test_predictfun_no_date_returns_none():
    adapter = PredictFunAdapter()
    assert adapter._parse_resolution_date(None) is None
    assert adapter._parse_resolution_date("") is None
    assert adapter._parse_resolution_date("resolves per official sources") is None
