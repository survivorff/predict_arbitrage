"""结算日期硬 veto 回归测试（Tier-0 红线 C「结算等价」）。

背景:Polymarket 同一事件下常有多个子市场共享**几乎相同的 question 文本**，真正
区分它们的是结算日期(``endDate``)。真实案例——外星人事件
"Will the US confirm that aliens exist before 2027?" 下有按月份分档的多个子市场
(endDate 2026-04-30 / 2026-06-30 / 2026-12-31 ...)。匹配只比标题文本时,
predict.fun 的某个市场可能错配到结算窗口完全不同的 Polymarket 子市场,制造跨不同
事件的虚假套利。

修复:``CanonicalMarket`` 增加 ``resolution_date`` 字段(适配器从平台数据解析),
``composite_score`` 在两市场都已知结算日期且相差超过阈值(7 天)时硬否决返回 0.0。

核心安全取向:日期 veto 只**阻止**匹配(错配→真亏钱),不**制造**匹配(漏配→只是
错过)。因此任一方日期未知时按中性处理(不否决),解析偏差最坏只导致漏配(安全方向)。

**Validates: Requirements 4.1, 4.2, 4.4(Tier-0 红线 C)**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from scanner.matching import LexicalSimilarity, composite_score, _date_mismatch
from scanner.models import CanonicalMarket, Outcome

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _yes_no() -> List[Outcome]:
    return [Outcome(name="Yes", price=0.5), Outcome(name="No", price=0.5)]


def _market(
    platform: str,
    title: str,
    *,
    resolution_date: Optional[datetime] = None,
) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=f"{platform}-{abs(hash((platform, title, resolution_date)))}",
        title=title,
        outcomes=_yes_no(),
        retrieved_at=NOW,
        resolution_date=resolution_date,
    )


# --------------------------------------------------------------------------- #
# _date_mismatch 直接行为
# --------------------------------------------------------------------------- #
def test_same_date_not_mismatch():
    a = _market("polymarket", "X", resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    b = _market("predictfun", "X", resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert _date_mismatch(a, b) is False


def test_within_threshold_not_mismatch():
    # 同一截止日的不同表述(23:59 ET vs 当日 00:00 UTC 等),相差 < 7 天,不否决。
    a = _market("polymarket", "X", resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    b = _market("predictfun", "X", resolution_date=datetime(2026, 12, 29, tzinfo=timezone.utc))
    assert _date_mismatch(a, b) is False


def test_different_month_is_mismatch():
    # 按月份分档的子市场(相差约 8 个月)→ 不同结算窗口 → 否决。
    a = _market("polymarket", "X", resolution_date=datetime(2026, 4, 30, tzinfo=timezone.utc))
    b = _market("predictfun", "X", resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert _date_mismatch(a, b) is True


def test_unknown_date_is_neutral():
    # 任一方日期未知 → 中性(不否决),避免因数据缺失而漏配。
    a = _market("polymarket", "X", resolution_date=datetime(2026, 4, 30, tzinfo=timezone.utc))
    b = _market("predictfun", "X", resolution_date=None)
    assert _date_mismatch(a, b) is False


def test_naive_datetime_handled():
    # naive datetime(无时区)应被当作 UTC,不报错。
    a = _market("polymarket", "X", resolution_date=datetime(2026, 4, 30))
    b = _market("predictfun", "X", resolution_date=datetime(2026, 12, 31))
    assert _date_mismatch(a, b) is True


# --------------------------------------------------------------------------- #
# composite_score 端到端:相同标题、不同结算日期 → 不匹配
# --------------------------------------------------------------------------- #
def test_identical_title_different_date_vetoed():
    """标题完全相同但结算日期相差数月 → composite_score 应为 0(被日期闸门拦下)。"""
    sim = LexicalSimilarity()
    title = "Will the US confirm that aliens exist before 2027?"
    a = _market("polymarket", title, resolution_date=datetime(2026, 4, 30, tzinfo=timezone.utc))
    b = _market("predictfun", title, resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert composite_score(a, b, similarity=sim) == 0.0


def test_identical_title_same_date_matches():
    """标题相同且结算日期一致(外星人单真实情形:都 2026-12-31)→ 高分匹配。"""
    sim = LexicalSimilarity()
    title = "Will the US confirm that aliens exist before 2027?"
    a = _market("polymarket", title, resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    b = _market("predictfun", title, resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert composite_score(a, b, similarity=sim) > 0.85


def test_identical_title_one_date_unknown_still_matches():
    """一方日期未知时不否决,仍按普通评分匹配(避免数据缺失导致漏配)。"""
    sim = LexicalSimilarity()
    title = "Will the US confirm that aliens exist before 2027?"
    a = _market("polymarket", title, resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc))
    b = _market("predictfun", title, resolution_date=None)
    assert composite_score(a, b, similarity=sim) > 0.85
