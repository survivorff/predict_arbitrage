"""信号质量切片 D 的语义/归一相似度测试（Phase Two）。

覆盖：
- SynonymNormalizer：变体->规范词、规范词->自身、未收录原样、批量、自定义 groups。
- EmbeddingSimilarity：余弦映射、空/零向量兜底、Protocol 一致性。
- HybridSimilarity：边界权重、加权融合、越界 ValueError、Protocol 一致性。
- NormalizingLexicalSimilarity：归一提升召回、无关项不被拉高、自定义归一器、Protocol。
- 可选端到端：MatchingEngine 用归一相似度把措辞不同的等价市场分到一组。

只写测试，不改实现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from scanner.matching import (
    EmbeddingSimilarity,
    HybridSimilarity,
    LexicalSimilarity,
    MatchingEngine,
    NormalizingLexicalSimilarity,
    SemanticSimilarity,
)
from scanner.models import CanonicalMarket, Outcome
from scanner.synonyms import DEFAULT_SYNONYM_GROUPS, SynonymNormalizer

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 确定性假编码器：把固定词映射到固定向量，未知词返回零向量。零外部依赖。
# ---------------------------------------------------------------------------

_VECTORS: Dict[str, List[float]] = {
    "a": [1.0, 0.0],
    "b": [0.0, 1.0],       # 与 a 正交
    "a2": [1.0, 0.0],      # 与 a 同向
    "neg_a": [-1.0, 0.0],  # 与 a 反向
}


def fake_encoder(text: str) -> Sequence[float]:
    """str -> 向量；未收录词返回零向量 [0, 0]。"""
    return _VECTORS.get(text, [0.0, 0.0])


class ConstantSemantic:
    """始终返回固定分数的假语义后端，便于断言融合结果。"""

    def __init__(self, value: float) -> None:
        self._value = value

    def score(self, a: str, b: str) -> float:
        return self._value


# ---------------------------------------------------------------------------
# SynonymNormalizer
# ---------------------------------------------------------------------------


def test_normalize_variant_maps_to_canonical():
    # 用 DEFAULT 词典：变体应归一到规范词。
    norm = SynonymNormalizer()
    assert norm.normalize("btc") == "bitcoin"
    assert norm.normalize("xbt") == "bitcoin"
    assert norm.normalize("potus") == "president"
    assert norm.normalize("presidential") == "president"
    assert norm.normalize("over") == "above"
    assert norm.normalize("exceed") == "above"
    assert norm.normalize("gop") == "republican"


def test_normalize_canonical_maps_to_itself():
    # 规范词自身映射到自身。
    norm = SynonymNormalizer()
    assert norm.normalize("bitcoin") == "bitcoin"
    assert norm.normalize("president") == "president"
    assert norm.normalize("above") == "above"


def test_normalize_unknown_token_returned_as_is():
    # 未收录词原样返回。
    norm = SynonymNormalizer()
    assert norm.normalize("lakers") == "lakers"
    assert norm.normalize("100000") == "100000"


def test_normalize_all_batch():
    # normalize_all 批量归一，保持顺序。
    norm = SynonymNormalizer()
    result = norm.normalize_all(["btc", "over", "lakers", "potus"])
    assert result == ["bitcoin", "above", "lakers", "president"]


def test_normalize_custom_groups():
    # 传入自定义 groups 时生效，且不再使用 DEFAULT。
    norm = SynonymNormalizer(groups={"x": ["y"]})
    assert norm.normalize("y") == "x"
    assert norm.normalize("x") == "x"
    # 自定义词典覆盖默认：btc 不再在词典中，原样返回。
    assert norm.normalize("btc") == "btc"


def test_default_groups_contains_expected_entries():
    # 简单确认默认词典结构符合预期。
    assert "btc" in DEFAULT_SYNONYM_GROUPS["bitcoin"]
    assert "potus" in DEFAULT_SYNONYM_GROUPS["president"]
    assert "over" in DEFAULT_SYNONYM_GROUPS["above"]


# ---------------------------------------------------------------------------
# EmbeddingSimilarity
# ---------------------------------------------------------------------------


def test_embedding_identical_vectors_score_one():
    # 同向向量：cos=1 -> (1+1)/2 = 1.0。
    sim = EmbeddingSimilarity(fake_encoder)
    assert sim.score("a", "a") == 1.0
    assert sim.score("a", "a2") == 1.0


def test_embedding_orthogonal_vectors_score_half():
    # 正交向量：cos=0 -> (0+1)/2 = 0.5。
    sim = EmbeddingSimilarity(fake_encoder)
    assert sim.score("a", "b") == 0.5


def test_embedding_opposite_vectors_score_zero():
    # 反向向量：cos=-1 -> (-1+1)/2 = 0.0。
    sim = EmbeddingSimilarity(fake_encoder)
    assert sim.score("a", "neg_a") == 0.0


def test_embedding_zero_or_empty_vector_returns_zero():
    # 未知词编码为零向量 -> 0.0。
    sim = EmbeddingSimilarity(fake_encoder)
    assert sim.score("unknown", "a") == 0.0
    assert sim.score("a", "unknown") == 0.0
    assert sim.score("unknown", "unknown") == 0.0

    # 编码器直接返回空向量也应兜底为 0.0。
    empty_sim = EmbeddingSimilarity(lambda text: [])
    assert empty_sim.score("a", "b") == 0.0


def test_embedding_satisfies_protocol():
    # runtime_checkable Protocol：实例应满足 SemanticSimilarity。
    sim = EmbeddingSimilarity(fake_encoder)
    assert isinstance(sim, SemanticSimilarity)


# ---------------------------------------------------------------------------
# HybridSimilarity
# ---------------------------------------------------------------------------


def test_hybrid_weight_one_equals_pure_lexical():
    # lexical_weight=1.0 时结果等于纯 lexical 分。
    lexical = LexicalSimilarity()
    semantic = ConstantSemantic(0.0)
    hybrid = HybridSimilarity(semantic, lexical=lexical, lexical_weight=1.0)
    a, b = "bitcoin above 100000", "bitcoin below 50000"
    assert hybrid.score(a, b) == lexical.score(a, b)


def test_hybrid_weight_zero_equals_pure_semantic():
    # lexical_weight=0.0 时结果等于纯 semantic 分。
    semantic = ConstantSemantic(0.42)
    hybrid = HybridSimilarity(semantic, lexical=LexicalSimilarity(), lexical_weight=0.0)
    assert hybrid.score("anything", "else") == 0.42


def test_hybrid_intermediate_weight_is_weighted_sum():
    # 中间权重：用固定值假后端断言加权和。
    # lex 分用纯 lexical 计算，sem 固定为 1.0，权重 0.25。
    lexical = LexicalSimilarity()
    semantic = ConstantSemantic(1.0)
    w = 0.25
    hybrid = HybridSimilarity(semantic, lexical=lexical, lexical_weight=w)
    a, b = "bitcoin above 100000", "bitcoin below 50000"
    expected = w * lexical.score(a, b) + (1.0 - w) * 1.0
    assert hybrid.score(a, b) == expected


def test_hybrid_default_lexical_backend():
    # 不传 lexical 时默认使用 LexicalSimilarity()。
    semantic = ConstantSemantic(0.0)
    hybrid = HybridSimilarity(semantic, lexical_weight=1.0)
    a, b = "bitcoin above 100000", "bitcoin above 100000"
    # 完全相同标题，纯词法分应为 1.0。
    assert hybrid.score(a, b) == 1.0


def test_hybrid_weight_out_of_range_raises():
    # lexical_weight 越界抛 ValueError。
    semantic = ConstantSemantic(0.5)
    import pytest

    with pytest.raises(ValueError):
        HybridSimilarity(semantic, lexical_weight=-0.1)
    with pytest.raises(ValueError):
        HybridSimilarity(semantic, lexical_weight=1.1)


def test_hybrid_satisfies_protocol():
    hybrid = HybridSimilarity(ConstantSemantic(0.5))
    assert isinstance(hybrid, SemanticSimilarity)


# ---------------------------------------------------------------------------
# NormalizingLexicalSimilarity（核心：提升召回 / 减少漏信号）
# ---------------------------------------------------------------------------


def test_normalizing_beats_plain_lexical_on_btc_synonym():
    # "BTC ... over ..." vs "Bitcoin ... above ..."：归一后分数严格更高且接近 1.0。
    a = "Will BTC close over 100000 in 2025"
    b = "Will Bitcoin close above 100000 in 2025"
    plain = LexicalSimilarity().score(a, b)
    normalized = NormalizingLexicalSimilarity().score(a, b)
    assert normalized > plain
    assert normalized >= 0.95


def test_normalizing_beats_plain_lexical_on_potus_synonym():
    # "POTUS election" vs "Presidential election"：归一后显著更高。
    a = "POTUS election"
    b = "Presidential election"
    plain = LexicalSimilarity().score(a, b)
    normalized = NormalizingLexicalSimilarity().score(a, b)
    assert normalized > plain
    assert normalized >= 0.95


def test_normalizing_keeps_unrelated_titles_low():
    # 完全无关的标题，归一不应把分数拉高。
    a = "Bitcoin price"
    b = "Lakers championship"
    normalized = NormalizingLexicalSimilarity().score(a, b)
    assert normalized < 0.3


def test_normalizing_custom_normalizer():
    # 自定义 normalizer 生效：把 "foo" 归一到 "bar"，使两标题等价。
    custom = SynonymNormalizer(groups={"bar": ["foo"]})
    sim = NormalizingLexicalSimilarity(normalizer=custom)
    a = "foo wins"
    b = "bar wins"
    plain = LexicalSimilarity().score(a, b)
    normalized = sim.score(a, b)
    assert normalized > plain
    assert normalized == 1.0


def test_normalizing_satisfies_protocol():
    sim = NormalizingLexicalSimilarity()
    assert isinstance(sim, SemanticSimilarity)
    # 它继承自 LexicalSimilarity。
    assert isinstance(sim, LexicalSimilarity)


# ---------------------------------------------------------------------------
# 端到端：MatchingEngine 用归一相似度把措辞不同的等价市场分到一组
# ---------------------------------------------------------------------------


def _market(platform: str, market_id: str, title: str) -> CanonicalMarket:
    return CanonicalMarket(
        platform=platform,
        market_id=market_id,
        title=title,
        outcomes=[Outcome(name="Yes", price=0.5), Outcome(name="No", price=0.5)],
        retrieved_at=NOW,
    )


def test_engine_normalizing_groups_synonym_phrased_markets():
    # 两个措辞不同（BTC/over vs Bitcoin/above）但等价的跨平台市场。
    poly = _market("polymarket", "p_btc", "Will BTC close over 100000 in 2025")
    kalshi = _market("kalshi", "k_btc", "Will Bitcoin close above 100000 in 2025")
    markets = [poly, kalshi]

    # 用归一相似度 + 较高阈值：应能分到一组。
    engine = MatchingEngine(
        similarity=NormalizingLexicalSimilarity(),
        score_threshold=0.8,
    )
    groups = engine.match(markets)
    assert len(groups) == 1
    assert len(groups[0].members) == 2

    # 同阈值下纯词法相似度的复合分更低，分不到组（确认归一确实提升召回）。
    plain_engine = MatchingEngine(
        similarity=LexicalSimilarity(),
        score_threshold=0.8,
    )
    assert plain_engine.match(markets) == []
