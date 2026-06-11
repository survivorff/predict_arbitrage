"""跨平台金标准关联匹配测试（Phase Three + 安全性修复）。

predict.fun 等平台会自报对端市场标识（``cross_refs``）。匹配引擎据此：

- ``_cross_referenced``：判定两个市场是否互指；
- ``composite_score``：互指提供强先验，但**不盲信**——用标题相似度做闸门
  （``title >= 0.85``）才给高置信，否则落回普通复合评分；
- ``Blocker.keys_for``：为 cross_refs 生成 ``xref:`` 分块键，让「A 引用 B」与
  「B 自身」落入同一候选块，即使标题措辞完全不同；
- ``MatchingEngine.match``：端到端把互指且标题相近的市场分到一组、高置信度，
  而互指但标题指向不同事件的对会被标题闸门拒绝（防止虚假套利）。

安全性修复背景：旧逻辑一旦发现 cross_ref 就短路返回 1.0（盲信），真实数据中
predict.fun 的 cross_ref 可能不精确（如把 "England win" 错关联到 "Germany win
Euros"），从而把不同事件当成同一事件、算出虚假套利。修复后标题闸门会拒绝这类
不一致的自报关联。

**Validates: Requirements 4.1, 4.2, 4.4**
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from scanner.matching import (
    Blocker,
    LexicalSimilarity,
    MatchingEngine,
    composite_score,
)
from scanner.matching import _cross_referenced
from scanner.models import CanonicalMarket, Outcome

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _yes_no() -> List[Outcome]:
    return [Outcome(name="Yes", price=0.5), Outcome(name="No", price=0.5)]


def market(
    platform: str,
    market_id: str,
    title: str,
    *,
    cross_refs: Optional[Dict[str, List[str]]] = None,
    outcomes: Optional[List[Outcome]] = None,
) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=outcomes if outcomes is not None else _yes_no(),
        retrieved_at=NOW,
        cross_refs=cross_refs or {},
    )


# --------------------------------------------------------------------------- #
# _cross_referenced
# --------------------------------------------------------------------------- #
def test_cross_referenced_forward():
    a = market("predictfun", "pf1", "A", cross_refs={"polymarket": ["P1"]})
    b = market("polymarket", "P1", "B")
    assert _cross_referenced(a, b) is True


def test_cross_referenced_reverse():
    # B 引用 A（反向）也应命中。
    a = market("polymarket", "P1", "A")
    b = market("predictfun", "pf1", "B", cross_refs={"polymarket": ["P1"]})
    assert _cross_referenced(a, b) is True


def test_cross_referenced_unrelated_is_false():
    a = market("predictfun", "pf1", "A", cross_refs={"polymarket": ["P1"]})
    b = market("polymarket", "P2", "B")
    assert _cross_referenced(a, b) is False


def test_cross_referenced_platform_mismatch_is_false():
    # id 相同但平台对不上：predictfun 引用的是 polymarket，而 b 是 kalshi。
    a = market("predictfun", "pf1", "A", cross_refs={"polymarket": ["P1"]})
    b = market("kalshi", "P1", "B")
    assert _cross_referenced(a, b) is False


# --------------------------------------------------------------------------- #
# composite_score 金标准提升（标题闸门）
# --------------------------------------------------------------------------- #
def test_composite_score_cross_referenced_with_similar_titles_high():
    # 标题相似 + 互指 → 融合 cross_ref 先验与标题证据，给出很高的分数。
    a = market(
        "predictfun",
        "pf1",
        "Will Bitcoin reach 100000 in 2025",
        cross_refs={"polymarket": ["P1"]},
    )
    b = market("polymarket", "P1", "Bitcoin reach 100000 in 2025")
    assert composite_score(a, b, similarity=LexicalSimilarity()) >= 0.9


def test_cross_ref_with_mismatched_titles_is_rejected():
    # 安全性核心用例：互指但标题指向**不同事件**（模拟真实 bug：predict.fun 的
    # cross_ref 把 "England win World Cup" 错关联到另一事件）。标题闸门应拒绝该
    # 不一致的自报关联——分数明显不再是盲目的 1.0，且端到端不分到一组。
    sim = LexicalSimilarity()
    # 先验证两个标题词法相似度确实 < 0.5（低于 TITLE_GATE，触发闸门回退）。
    title_a = "Will England win the 2026 World Cup"
    title_b = "Will Germany win the 2024 Euros"
    assert sim.score(title_a, title_b) < 0.5

    a = market("polymarket", "P1", title_a, outcomes=_yes_no())
    b = market(
        "predictfun",
        "pf1",
        title_b,
        cross_refs={"polymarket": ["P1"]},
        outcomes=_yes_no(),
    )

    # 标题闸门拒绝金标准加成，分数落回普通复合评分（远低于盲信的 1.0）。
    assert composite_score(a, b, similarity=sim) < 0.9

    # 端到端：阈值 0.8 下，不一致的互指对不应分到一组（或置信度 < 0.8）。
    groups = MatchingEngine(score_threshold=0.8).match([a, b])
    if groups:
        assert all(g.match_confidence < 0.8 for g in groups)
    else:
        assert groups == []


def test_composite_score_without_crossref_below_one_for_different_titles():
    a = market("predictfun", "pf1", "完全不相干的措辞 alpha bravo charlie")
    b = market("polymarket", "P1", "totally different wording delta echo")
    assert composite_score(a, b, similarity=LexicalSimilarity()) < 1.0


def test_cross_ref_group_stage_vs_championship_rejected():
    # 真实数据回归用例（虚假套利根因）：predict.fun 的「赢小组H」市场错误地把
    # cross_ref 指向 Polymarket 的「夺冠」市场。两标题词法相似度=0.765（共享
    # Spain/win/2026/World/Cup），介于普通错配与真同一事件之间，恰好考验
    # TITLE_GATE(0.85) 能否区分「夺冠 vs 小组赛」。
    sim = LexicalSimilarity()
    title_a = "Will Spain win the 2026 FIFA World Cup?"  # Polymarket：西班牙夺冠
    title_b = "Will Spain win Group H in the 2026 World Cup"  # predict.fun：赢小组 H

    # 锚定实测相似度：0.765 < 0.85（应触发闸门回退），且明显高于无关标题，
    # 证明仅靠 title>=0.5 的旧闸门拦不住、必须提高到 0.85。
    measured = sim.score(title_a, title_b)
    assert 0.5 <= measured < 0.85

    a = market("polymarket", "P1", title_a, outcomes=_yes_no())
    b = market(
        "predictfun",
        "30985",
        title_b,
        cross_refs={"polymarket": ["P1"]},
        outcomes=_yes_no(),
    )

    # 不再因 cross_ref 盲目给高分：落回普通复合评分，低于 0.8 阈值。
    assert composite_score(a, b, similarity=sim) < 0.8

    # 端到端：阈值 0.8 下，「夺冠 vs 小组赛」这对不应分到一组（杜绝虚假套利）。
    groups = MatchingEngine(score_threshold=0.8).match([a, b])
    if groups:
        assert all(g.match_confidence < 0.8 for g in groups)
    else:
        assert groups == []


# --------------------------------------------------------------------------- #
# Blocker.keys_for
# --------------------------------------------------------------------------- #
def test_keys_for_includes_self_and_ref_xref_keys():
    m = market(
        "predictfun",
        "pf1",
        "BTC above 100k",
        cross_refs={"polymarket": ["P1"], "kalshi": ["KX-T"]},
    )
    keys = Blocker().keys_for(m)
    # 指向自己的键。
    assert "xref:predictfun:pf1" in keys
    # 引用对端的键。
    assert "xref:polymarket:P1" in keys
    assert "xref:kalshi:KX-T" in keys


# --------------------------------------------------------------------------- #
# MatchingEngine.match 端到端
# --------------------------------------------------------------------------- #
def test_match_groups_cross_referenced_with_similar_titles():
    # 标题相近 + 互指 → 分到一组、高置信度。
    pf = market(
        "predictfun",
        "pf1",
        "Will Bitcoin reach 100000 in 2025",
        cross_refs={"polymarket": ["P1"]},
    )
    poly = market("polymarket", "P1", "Bitcoin reach 100000 in 2025")

    engine = MatchingEngine(score_threshold=0.8)
    groups = engine.match([pf, poly])

    assert len(groups) == 1
    group = groups[0]
    assert {m.market_id for m in group.members} == {"pf1", "P1"}
    assert group.match_confidence >= 0.9


def test_match_does_not_group_unrelated_markets_with_different_titles():
    # 无 cross_refs 且标题措辞完全不同 → 不分组（阈值 0.8）。
    pf = market("predictfun", "pf1", "完全不同措辞的标题 alpha bravo")
    poly = market("polymarket", "P1", "an entirely unrelated phrasing charlie delta")

    engine = MatchingEngine(score_threshold=0.8)
    assert engine.match([pf, poly]) == []


# --------------------------------------------------------------------------- #
# 赛段/范围限定词闸门（真实虚假套利回归：夺冠 vs 赢小组）
# --------------------------------------------------------------------------- #

def test_scope_gate_rejects_win_cup_vs_win_group_real_case():
    """真实案例回归：「Will Mexico win the 2026 FIFA World Cup?」(夺冠) 与
    「Will Mexico win Group A in the 2026 FIFA World Cup?」(赢A组) 标题词法相似度
    高达约 0.86，旧逻辑误判等价、算出 122% 虚假套利。赛段闸门应判二者不等价（分 0）。
    """
    poly = market("polymarket", "P1", "Will Mexico win the 2026 FIFA World Cup?")
    pf = market("predictfun", "pf1", "Will Mexico win Group A in the 2026 FIFA World Cup?")
    score = composite_score(poly, pf, similarity=LexicalSimilarity())
    assert score == 0.0


def test_scope_gate_rejects_even_with_cross_ref():
    """即便平台自报 cross_ref 互指，赛段不一致仍判不等价（保护金标准路径）。"""
    poly = market("polymarket", "P1", "Will Mexico win the 2026 FIFA World Cup?")
    pf = market("predictfun", "pf1", "Will Mexico win Group A in the 2026 FIFA World Cup?",
                cross_refs={"polymarket": ["P1"]})
    assert composite_score(pf, poly, similarity=LexicalSimilarity()) == 0.0


def test_scope_gate_keeps_genuine_outright_match():
    """正当的同一事件（都是夺冠，措辞不同）不应被赛段闸门误伤。"""
    a = market("polymarket", "P1", "Will Mexico win the 2026 FIFA World Cup?")
    b = market("predictfun", "pf1", "Mexico to win the 2026 World Cup")
    assert composite_score(a, b, similarity=LexicalSimilarity()) > 0.0


def test_scope_gate_keeps_same_group_match():
    """都是赢得 A 组（同一事件）应保留。"""
    a = market("polymarket", "P1", "Will Mexico win Group A in the 2026 FIFA World Cup?")
    b = market("predictfun", "pf1", "Mexico to win Group A at the 2026 World Cup")
    assert composite_score(a, b, similarity=LexicalSimilarity()) > 0.0


def test_scope_gate_rejects_different_groups():
    """不同组别（A 组 vs B 组）是不同事件，应判不等价。"""
    a = market("polymarket", "P1", "Will Mexico win Group A in the 2026 FIFA World Cup?")
    b = market("predictfun", "pf1", "Will Mexico win Group B in the 2026 FIFA World Cup?")
    assert composite_score(a, b, similarity=LexicalSimilarity()) == 0.0


# --------------------------------------------------------------------------- #
# 数字/阈值/日期限定词闸门（同类 bug 防御：不同阈值/年份不可视为同一事件）
# --------------------------------------------------------------------------- #

def test_number_gate_rejects_different_counts():
    """「4 次降息」vs「7 次降息」是不同阈值 → 不同事件。"""
    a = market("polymarket", "P1", "Will 4 Fed rate cuts happen in 2026?")
    b = market("predictfun", "pf1", "Will 7 Fed rate cuts happen in 2026?")
    assert composite_score(a, b, similarity=LexicalSimilarity()) == 0.0


def test_number_gate_rejects_different_strikes():
    """「BTC 上 $100k」vs「上 $110k」是不同行权价 → 不同事件。"""
    a = market("polymarket", "P1", "Will Bitcoin close above $100,000 in 2025?")
    b = market("predictfun", "pf1", "Will Bitcoin close above $110,000 in 2025?")
    assert composite_score(a, b, similarity=LexicalSimilarity()) == 0.0


def test_number_gate_rejects_different_years():
    """不同结算年份 → 不同事件。"""
    a = market("polymarket", "P1", "Will Bitcoin hit 100k by 2025?")
    b = market("predictfun", "pf1", "Will Bitcoin hit 100k by 2026?")
    assert composite_score(a, b, similarity=LexicalSimilarity()) == 0.0


def test_number_gate_allows_same_value_different_format():
    """同值不同格式（$100,000 vs $100k）不应被误拒。"""
    a = market("polymarket", "P1", "Will Bitcoin close above $100,000 in 2025?")
    b = market("predictfun", "pf1", "Will Bitcoin close above $100k in 2025?")
    assert composite_score(a, b, similarity=LexicalSimilarity()) > 0.0


def test_number_gate_does_not_misparse_b_in_word():
    """回归：'100000 by 2025' 的 'b' 不应被当作十亿后缀而误拒同值匹配。"""
    a = market("polymarket", "P1", "Will Bitcoin close above 100000 in 2025")
    b = market("kalshi", "k1", "Bitcoin above 100000 by 2025")
    assert composite_score(a, b, similarity=LexicalSimilarity()) > 0.0
