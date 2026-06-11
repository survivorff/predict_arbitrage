"""Cross-platform market matching: blocking and similarity (Req 4.1).

This module implements the *first tier* of the tiered MatchingEngine described
in the design: candidate generation via **blocking** and a pluggable
**semantic similarity** interface with a cheap lexical default.

Scope (task 9.1):

- ``SemanticSimilarity`` — a Protocol every similarity backend satisfies, plus a
  lexical default (``LexicalSimilarity``) using a token-set ratio so the system
  runs without an external embedding model and can be upgraded later.
- ``Blocker`` — reduces the O(N^2) pairwise comparison space by bucketing
  markets on coarse keys (entities extracted from the title, plus any available
  category / close-date signal) and only pairing markets that share a block and
  come from *different* platforms (Req 4.1).

The scoring/outcome-mapping/confidence tiers build on these primitives in task
9.2; this module is intentionally limited to candidate generation and the
similarity contract, but is structured so the later tiers can consume it.

Everything here is deterministic given its inputs, so it is unit-testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    runtime_checkable,
)

from scanner.models import (
    CanonicalMarket,
    EquivalentMarketGroup,
    Outcome,
    OutcomeAlignment,
)
from scanner.synonyms import SynonymNormalizer

# ---------------------------------------------------------------------------
# Text normalization helpers
# ---------------------------------------------------------------------------

# Common English words that carry little discriminating signal for matching
# market titles. Kept small and deterministic; the goal is to drop filler so
# that entity tokens (names, numbers, thresholds) drive blocking.
STOPWORDS: FrozenSet[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "their",
        "this",
        "to",
        "was",
        "will",
        "with",
        "than",
        "that",
        "what",
        "when",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "market",
        "markets",
        "event",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    """Lowercase ``text`` and collapse non-alphanumeric runs to single spaces."""
    return " ".join(_TOKEN_RE.findall(text.lower()))


def tokenize(text: str) -> List[str]:
    """Split ``text`` into lowercased alphanumeric tokens (order preserved)."""
    return _TOKEN_RE.findall(text.lower())


def _is_number(token: str) -> bool:
    return token.isdigit()


def extract_entities(text: str, *, min_token_length: int = 4) -> Set[str]:
    """Extract coarse entity tokens used as blocking keys.

    "Entities" here are the discriminating tokens of a market title: numbers
    (years, price thresholds like ``4000``) and significant words (names,
    teams, tickers) once stopwords and very short words are removed.

    The result is intentionally lossy and order-independent so that two phrasings
    of the same event ("Will Bitcoin close above $100,000 in 2025?" vs.
    "Bitcoin above 100000 by 2025") share entity tokens and land in a common
    block.
    """
    entities: Set[str] = set()
    for token in tokenize(text):
        if _is_number(token):
            entities.add(token)
        elif token not in STOPWORDS and len(token) >= min_token_length:
            entities.add(token)
    return entities


# ---------------------------------------------------------------------------
# Semantic similarity interface + lexical default
# ---------------------------------------------------------------------------


@runtime_checkable
class SemanticSimilarity(Protocol):
    """Scores the semantic closeness of two market titles in ``[0, 1]``.

    The MatchingEngine depends on this contract rather than any concrete
    backend, so an embedding-based implementation can later be dropped in
    without touching candidate generation or scoring. ``1.0`` means identical /
    certainly the same; ``0.0`` means unrelated.
    """

    def score(self, a: str, b: str) -> float:
        """Return a similarity score in ``[0, 1]`` for titles ``a`` and ``b``."""
        ...


def _ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio in [0, 1]; 1.0 for two empty strings."""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class LexicalSimilarity:
    """A deterministic, dependency-free lexical ``SemanticSimilarity``.

    The score blends two order-independent signals:

    - a *token-set ratio* (the strategy popularized by fuzzy-string matching
      libraries): tokens shared by both titles are factored out and the leftover
      tokens on each side are compared against that shared core, making the
      score robust to word order and to one title carrying extra qualifiers; and
    - the *Jaccard overlap* of the two token sets, which collapses toward zero
      when the titles share no tokens.

    Blending the two means identical token *sets* score ``1.0`` while titles
    with no shared tokens score low even when their characters happen to
    overlap (the known weakness of a pure character-level ratio).
    """

    def __init__(self, *, min_token_length: int = 1, drop_stopwords: bool = True) -> None:
        self.min_token_length = min_token_length
        self.drop_stopwords = drop_stopwords

    def _token_set(self, text: str) -> Set[str]:
        tokens = tokenize(text)
        result: Set[str] = set()
        for tok in tokens:
            if len(tok) < self.min_token_length:
                continue
            if self.drop_stopwords and tok in STOPWORDS:
                continue
            result.add(tok)
        return result

    def score(self, a: str, b: str) -> float:
        set_a = self._token_set(a)
        set_b = self._token_set(b)

        if not set_a and not set_b:
            # Both reduce to nothing meaningful; treat as identical.
            return 1.0
        if not set_a or not set_b:
            return 0.0

        intersection = set_a & set_b
        diff_a = set_a - set_b
        diff_b = set_b - set_a

        sorted_inter = " ".join(sorted(intersection))
        combined_a = (sorted_inter + " " + " ".join(sorted(diff_a))).strip()
        combined_b = (sorted_inter + " " + " ".join(sorted(diff_b))).strip()

        token_set_ratio = max(
            _ratio(sorted_inter, combined_a),
            _ratio(sorted_inter, combined_b),
            _ratio(combined_a, combined_b),
        )

        # Jaccard overlap of token sets: 0.0 when the titles share no tokens,
        # which damps the character-level ratio for unrelated titles.
        jaccard = len(intersection) / len(set_a | set_b)

        # Equal blend keeps identical token sets at 1.0 (both terms are 1.0)
        # while pulling zero-overlap pairs down toward the ratio's floor.
        return 0.5 * token_set_ratio + 0.5 * jaccard


class EmbeddingSimilarity:
    """可插拔的嵌入式语义相似度（Phase Two · 切片 D）。

    通过**注入的编码器** ``encoder`` 计算两个标题向量的余弦相似度，把分数映射到
    ``[0, 1]``。这样系统不强依赖任何具体的嵌入库——调用方可注入
    sentence-transformers、OpenAI embedding 或任意 ``str -> 向量`` 的函数；测试可
    注入确定性假编码器以保持离线、可复现。

    余弦相似度本在 ``[-1, 1]``，这里线性映射到 ``[0, 1]``（``(cos + 1) / 2``），
    与 ``SemanticSimilarity`` 契约一致：``1.0`` 表示同向（语义相同），``0.0`` 表示反向。
    若任一标题编码为零向量（或编码器对空串返回零向量），返回 ``0.0``。
    """

    def __init__(self, encoder: Callable[[str], "Sequence[float]"]) -> None:
        self._encoder = encoder

    def score(self, a: str, b: str) -> float:
        va = list(self._encoder(a))
        vb = list(self._encoder(b))
        if not va or not vb or len(va) != len(vb):
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb))
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(y * y for y in vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        cosine = dot / (na * nb)
        # 线性映射 [-1,1] -> [0,1]，并 clamp 抵御浮点误差。
        return max(0.0, min(1.0, (cosine + 1.0) / 2.0))


class HybridSimilarity:
    """词法 + 语义的加权融合（Phase Two · 切片 D）。

    把 :class:`LexicalSimilarity`（确定、离线、零依赖）与一个语义后端
    （:class:`EmbeddingSimilarity` 或任意 ``SemanticSimilarity``）按权重融合，
    兼顾召回（语义能匹配措辞差异大的等价市场）与稳健兜底（词法）。

    ``lexical_weight`` ∈ [0, 1]；语义权重为 ``1 - lexical_weight``。两个后端的分数都
    在 [0, 1]，故融合结果也在 [0, 1]。
    """

    def __init__(
        self,
        semantic: "SemanticSimilarity",
        *,
        lexical: "Optional[SemanticSimilarity]" = None,
        lexical_weight: float = 0.5,
    ) -> None:
        if not 0.0 <= lexical_weight <= 1.0:
            raise ValueError("lexical_weight must be within [0, 1]")
        self._semantic = semantic
        self._lexical = lexical if lexical is not None else LexicalSimilarity()
        self._lexical_weight = lexical_weight

    def score(self, a: str, b: str) -> float:
        lex = self._lexical.score(a, b)
        sem = self._semantic.score(a, b)
        w = self._lexical_weight
        return max(0.0, min(1.0, w * lex + (1.0 - w) * sem))


class NormalizingLexicalSimilarity(LexicalSimilarity):
    """带同义词/缩写归一的词法相似度（Phase Two · 切片 D 信号质量）。

    在 :class:`LexicalSimilarity` 的基础上，分词后先经 :class:`SynonymNormalizer`
    把同义/缩写词归一到规范词（如 ``btc``→``bitcoin``、``potus``→``president``），
    再做 token-set 比较。这样措辞不同但语义等价的跨平台市场能获得更高相似度，**减少
    漏信号**，且不引入任何外部依赖（确定、离线）。

    它是默认 ``LexicalSimilarity`` 的「即插即用」增强：在不具备嵌入模型时即可提升召回；
    需要更强语义时仍可换用 :class:`EmbeddingSimilarity` / :class:`HybridSimilarity`。
    """

    def __init__(
        self,
        *,
        normalizer: "Optional[SynonymNormalizer]" = None,
        min_token_length: int = 1,
        drop_stopwords: bool = True,
    ) -> None:
        super().__init__(min_token_length=min_token_length, drop_stopwords=drop_stopwords)
        self._normalizer = normalizer if normalizer is not None else SynonymNormalizer()

    def _token_set(self, text: str) -> Set[str]:
        # 复用父类的分词/停用词逻辑，再把每个词条归一到规范词。
        base = super()._token_set(text)
        return {self._normalizer.normalize(tok) for tok in base}


# ---------------------------------------------------------------------------
# Blocking / candidate generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidatePair:
    """An ordered pair of markets from different platforms to be scored later.

    Ordering is canonical (by ``(platform, market_id)``) so that a given
    unordered pair is represented exactly once regardless of input order.
    """

    a: CanonicalMarket
    b: CanonicalMarket

    @property
    def key(self) -> Tuple[Tuple[str, str], Tuple[str, str]]:
        return (
            (self.a.platform, self.a.market_id),
            (self.b.platform, self.b.market_id),
        )


def _market_key(market: CanonicalMarket) -> Tuple[str, str]:
    return (market.platform, market.market_id)


# A function that derives extra blocking keys from a market (e.g. a normalized
# category or close-date bucket). The canonical model does not carry those
# fields in Phase One, so the default derives keys from the title alone; callers
# with richer metadata can supply additional extractors.
KeyExtractor = Callable[[CanonicalMarket], Iterable[str]]


@dataclass
class Blocker:
    """Generates candidate pairs by bucketing markets on coarse keys (Req 4.1).

    Two markets become a candidate pair iff they share at least one blocking key
    *and* originate from different platforms. This collapses the full O(N^2)
    cross product to comparisons that have a realistic chance of matching.

    Blocking keys per market are the union of:

    - entity tokens extracted from the title (numbers + significant words), and
    - any keys produced by the optional ``extra_extractors`` (e.g. a normalized
      category or a close/expiry-date bucket when that data is available).

    Markets with no extractable keys produce no candidate pairs, which keeps
    junk titles from exploding the comparison space.
    """

    min_token_length: int = 4
    extra_extractors: List[KeyExtractor] = field(default_factory=list)

    def keys_for(self, market: CanonicalMarket) -> Set[str]:
        """Return the set of blocking keys for ``market`` (Req 4.1)."""
        keys: Set[str] = {
            "entity:" + e
            for e in extract_entities(market.title, min_token_length=self.min_token_length)
        }
        # 跨平台金标准关联键（Phase Three）：让平台自报互指的市场落入同一候选块，
        # 即使标题措辞完全不同。每个市场生成「指向自己」的键，以及它「引用对端」的键；
        # 当 A 的 cross_refs 指向 B 时，A 产生 xref:{B平台}:{B_id}，与 B 自身产生的
        # xref:{B平台}:{B_id} 命中同一键。
        keys.add(f"xref:{market.platform}:{market.market_id}")
        for platform, ids in market.cross_refs.items():
            for ref_id in ids:
                keys.add(f"xref:{platform}:{ref_id}")
        for extractor in self.extra_extractors:
            for raw in extractor(market):
                if raw is None:
                    continue
                value = str(raw).strip().lower()
                if value:
                    keys.add(value)
        return keys

    def blocks(self, markets: Iterable[CanonicalMarket]) -> Dict[str, List[CanonicalMarket]]:
        """Bucket ``markets`` by blocking key.

        Returns a mapping ``block_key -> markets sharing that key``. A market
        appears in every bucket whose key it carries.
        """
        buckets: Dict[str, List[CanonicalMarket]] = {}
        for market in markets:
            for key in self.keys_for(market):
                buckets.setdefault(key, []).append(market)
        return buckets

    def candidate_pairs(
        self, markets: Iterable[CanonicalMarket]
    ) -> List[CandidatePair]:
        """Return deduplicated cross-platform candidate pairs (Req 4.1).

        Only markets that share a block and come from different platforms are
        paired. Each unordered pair is returned once, in a deterministic order
        sorted by canonical market key.
        """
        market_list = list(markets)
        buckets = self.blocks(market_list)

        seen: Set[Tuple[Tuple[str, str], Tuple[str, str]]] = set()
        pairs: List[CandidatePair] = []

        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    m1, m2 = bucket[i], bucket[j]
                    # Req 4.1: only compare markets from *different* platforms.
                    if m1.platform == m2.platform:
                        continue
                    # Canonical ordering so the pair is identity-stable.
                    if _market_key(m1) <= _market_key(m2):
                        first, second = m1, m2
                    else:
                        first, second = m2, m1
                    identity = (_market_key(first), _market_key(second))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    pairs.append(CandidatePair(a=first, b=second))

        pairs.sort(key=lambda p: p.key)
        return pairs


def all_cross_platform_pairs(
    markets: Iterable[CanonicalMarket],
) -> List[CandidatePair]:
    """The unblocked baseline: every cross-platform pair.

    Provided so tests (and future evaluation) can quantify how much blocking
    reduces the candidate space relative to the full cross product.
    """
    market_list = list(markets)
    seen: Set[Tuple[Tuple[str, str], Tuple[str, str]]] = set()
    pairs: List[CandidatePair] = []
    for i in range(len(market_list)):
        for j in range(i + 1, len(market_list)):
            m1, m2 = market_list[i], market_list[j]
            if m1.platform == m2.platform:
                continue
            if _market_key(m1) <= _market_key(m2):
                first, second = m1, m2
            else:
                first, second = m2, m1
            identity = (_market_key(first), _market_key(second))
            if identity in seen:
                continue
            seen.add(identity)
            pairs.append(CandidatePair(a=first, b=second))
    pairs.sort(key=lambda p: p.key)
    return pairs


# ---------------------------------------------------------------------------
# Scoring tier (task 9.2): composite similarity, outcome mapping, confidence
# ---------------------------------------------------------------------------

# Tokens that denote a YES-style (affirmative) outcome and a NO-style
# (negation) outcome. Used to align outcomes across platforms even when they
# phrase the binary question differently.
_YES_TOKENS: FrozenSet[str] = frozenset({"yes", "y", "true", "win", "wins", "above", "over"})
_NO_TOKENS: FrozenSet[str] = frozenset({"no", "n", "false", "lose", "loses", "below", "under"})


def _outcome_polarity(name: str) -> Optional[bool]:
    """Classify an outcome name as affirmative (True), negation (False), or unknown.

    Returns ``True`` for YES-style outcomes, ``False`` for NO-style outcomes and
    ``None`` when the name carries no recognizable polarity. The first
    recognized token in the name decides the polarity, so "Yes, above 4000"
    classifies as affirmative.
    """
    for token in tokenize(name):
        if token in _YES_TOKENS:
            return True
        if token in _NO_TOKENS:
            return False
    return None


def _is_binary_yes_no(market: CanonicalMarket) -> bool:
    """True when ``market`` has exactly two outcomes with YES/NO polarity."""
    if len(market.outcomes) != 2:
        return False
    polarities = {_outcome_polarity(o.name) for o in market.outcomes}
    return polarities == {True, False}


def _close_date(market: CanonicalMarket) -> Optional[datetime]:
    """Best-effort extraction of a market's resolution/close date.

    现在 :class:`CanonicalMarket` 带有结构化的 ``resolution_date`` 字段（由各适配器从
    平台数据解析填充），优先使用它；并保留对旧式属性名的兼容回退，便于测试构造的轻量
    对象沿用旧约定。返回 ``None`` 时日期维度按中性处理（不奖励也不惩罚）。
    """
    primary = getattr(market, "resolution_date", None)
    if isinstance(primary, datetime):
        return primary
    for attr in ("close_date", "close_time", "end_date"):
        value = getattr(market, attr, None)
        if isinstance(value, datetime):
            return value
    return None


def _date_proximity(a: CanonicalMarket, b: CanonicalMarket) -> float:
    """Score resolution/close-date closeness in ``[0, 1]`` (1.0 == same day).

    When either close date is unavailable the signal is neutral (``0.5``) so it
    neither rewards nor penalizes the pair. Otherwise the score decays linearly
    from 1.0 (same instant) to 0.0 at a 30-day separation.
    """
    da, db = _close_date(a), _close_date(b)
    if da is None or db is None:
        return 0.5
    delta_days = abs((da - db).total_seconds()) / 86400.0
    horizon_days = 30.0
    if delta_days >= horizon_days:
        return 0.0
    return 1.0 - (delta_days / horizon_days)


def _entity_overlap(a: CanonicalMarket, b: CanonicalMarket) -> float:
    """Jaccard overlap of the two titles' entity tokens in ``[0, 1]``."""
    ea = extract_entities(a.title)
    eb = extract_entities(b.title)
    if not ea and not eb:
        return 1.0
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / len(ea | eb)


# Composite-score weights. They sum to 1.0 so the composite stays in [0, 1]
# (Property 7). Title similarity dominates; structure compatibility and entity
# overlap are strong secondary signals; date proximity is a lighter tie-breaker.
@dataclass(frozen=True)
class ScoreWeights:
    title: float = 0.5
    entity_overlap: float = 0.25
    structure: float = 0.15
    date_proximity: float = 0.10

    def normalized(self) -> "ScoreWeights":
        total = self.title + self.entity_overlap + self.structure + self.date_proximity
        if total <= 0:
            raise ValueError("score weights must sum to a positive number")
        return ScoreWeights(
            title=self.title / total,
            entity_overlap=self.entity_overlap / total,
            structure=self.structure / total,
            date_proximity=self.date_proximity / total,
        )


@dataclass(frozen=True)
class ScoredPair:
    """A candidate pair with its composite similarity score in ``[0, 1]``."""

    pair: CandidatePair
    score: float


def _cross_referenced(a: CanonicalMarket, b: CanonicalMarket) -> bool:
    """两个市场是否通过平台自报的 ``cross_refs`` 互指（金标准关联，Phase Three）。

    例如 predict.fun 的市场在 ``cross_refs["polymarket"]`` 中列出其对应的 Polymarket
    conditionId；若该值等于对端 Polymarket 市场的 ``market_id``，即为平台权威认定的
    同一事件，远比标题语义可靠。任一方向命中即视为关联。
    """
    def _refs_to(src: CanonicalMarket, dst: CanonicalMarket) -> bool:
        ids = src.cross_refs.get(dst.platform)
        return bool(ids) and dst.market_id in set(ids)
    return _refs_to(a, b) or _refs_to(b, a)


# 赛段/范围限定词：决定一个预测市场「问的是哪一层事件」的关键词。两个市场只有在
# 这些限定词上一致，才可能是同一事件。例如「夺得世界杯冠军」(outright) 与「赢得 A 组」
# (group stage) 标题几乎相同（仅多 "Group A in"），词法相似度高达 ~0.86，但它们是
# **完全不同的事件**——真实数据中正是这种错配制造了 122% 的虚假套利。
#
# 设计取向：把每个标题归一为一个「赛段签名」(scope signature)；签名不一致即判定不同
# 事件、强制低分（保护 cross_ref 与普通评分两条路径）。刻意保守，只收录会**实质改变
# 问题含义**的限定词，避免误伤正当匹配。

# 小组赛：捕获 "group a".."group z"（带字母则区分组别）或裸 "group"/"groups"。
_GROUP_LETTER_RE = re.compile(r"\bgroup\s+([a-z])\b")
_GROUP_BARE_RE = re.compile(r"\bgroups?\b")
# 淘汰赛轮次与进程限定词。
_STAGE_PATTERNS = {
    "ro16": re.compile(r"\bround of 16\b"),
    "ro32": re.compile(r"\bround of 32\b"),
    "quarterfinal": re.compile(r"\bquarter[\s-]?final"),
    "semifinal": re.compile(r"\bsemi[\s-]?final"),
    "advance": re.compile(r"\badvance|\bqualify"),
}


def _scope_signature(title: str) -> frozenset:
    """把标题归一为「赛段/范围签名」——决定其问的是哪一层事件的限定词集合。

    无任何限定词 → 空集（通常表示「夺冠/赢得整体」outright）。检测到小组赛/淘汰赛
    轮次等则计入对应标记；小组带字母时计入 ``group:<字母>`` 以区分不同组别。
    """
    t = title.lower()
    markers = set()
    m = _GROUP_LETTER_RE.search(t)
    if m:
        markers.add(f"group:{m.group(1)}")
    elif _GROUP_BARE_RE.search(t):
        markers.add("group")
    for name, pat in _STAGE_PATTERNS.items():
        if pat.search(t):
            markers.add(name)
    return frozenset(markers)


def _scope_mismatch(a: CanonicalMarket, b: CanonicalMarket) -> bool:
    """两个市场的赛段签名是否不一致（→ 不同事件，不可视为等价）。

    规则：
    1. 淘汰赛/进程标记（ro16/ro32/quarterfinal/semifinal/advance）必须完全一致。
    2. 「是否小组赛层级」必须一致：一方小组赛、另一方非小组赛（如夺冠）→ 不同事件。
    3. 双方都给出具体组别字母时，字母必须一致（A 组 ≠ B 组）；一方裸 "group"、
       另一方带字母视为兼容（同属小组赛层级，避免误拒同义表述）。
    """
    def _split(title: str):
        sig = _scope_signature(title)
        letters = {x.split(":", 1)[1] for x in sig if x.startswith("group:")}
        has_group = ("group" in sig) or bool(letters)
        stages = {x for x in sig if x != "group" and not x.startswith("group:")}
        return letters, has_group, stages

    la, ga, sta = _split(a.title)
    lb, gb, stb = _split(b.title)
    if sta != stb:
        return True            # 淘汰赛/进程标记不同
    if ga != gb:
        return True            # 小组赛 vs 非小组赛（如 夺冠 vs 赢小组）
    if la and lb and la != lb:
        return True            # 都指明组别但组别不同（A 组 vs B 组）
    return False


# 数字/阈值/日期：预测市场标题里的关键数字（行权价 $100k、次数 4 次、年份 2025）
# 往往是**问题含义的决定性事实**。它们不同 → 不同问题（如「4 次降息」vs「7 次降息」、
# 「BTC 上 $100k」vs「上 $110k」、「2025 年」vs「2026 年」）。但纯词法相似度里这些
# 数字只占一个 token 的权重，差异被淹没——真实数据中这类错配评分高达 0.79，逼近阈值。
# 因此把「显著数字集合不一致」也作为硬否决（与赛段闸门同思路）。
_NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:k|m|bn|b|bps|%)?", re.IGNORECASE)
_SUFFIX_MULT = {"k": 1e3, "m": 1e6, "bn": 1e9, "b": 1e9}


def _normalize_number(token: str) -> Optional[float]:
    """把标题里的数字 token 归一为可比数值：去 $/逗号，处理 k/m/b/bps/% 后缀。"""
    t = token.strip().lower().lstrip("$").replace(",", "")
    mult = 1.0
    for suf, m in _SUFFIX_MULT.items():
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
            mult = m
            break
    else:
        for suf in ("bps", "%"):
            if t.endswith(suf):
                t = t[: -len(suf)].strip()
                break
    try:
        return float(t) * mult
    except ValueError:
        return None


def _significant_numbers(title: str) -> frozenset:
    """提取标题中的显著数字集合（行权价/次数/年份等），归一后用于比较。"""
    out = set()
    for m in _NUM_RE.findall(title):
        v = _normalize_number(m)
        if v is not None:
            out.add(v)
    return frozenset(out)


def _number_mismatch(a: CanonicalMarket, b: CanonicalMarket) -> bool:
    """两个标题的显著数字集合是否不一致（→ 不同阈值/日期 → 不同事件）。

    保守策略：只要数字集合不同即否决（如 {4} vs {7}、{100000,2025} vs {110000,2025}、
    {2025} vs {2026}）。宁可漏配（不亏钱）也不错配（会亏钱）。
    """
    return _significant_numbers(a.title) != _significant_numbers(b.title)


# 结算日期闸门（Tier-0 红线 C「结算等价」）：标题几乎相同、但**结算窗口不同**的两个
# 市场是不同事件。真实案例:Polymarket 同一事件下有多个子市场共享几乎相同的 question
# 文本（"Will the US confirm that aliens exist before 2027?"），真正区分它们的是结算
# 日期（endDate 分别为 2026-04-30 / 2026-06-30 / 2026-12-31 ...）。匹配只比标题文本
# 时，predict.fun 的某个市场可能错配到结算窗口完全不同的 Polymarket 子市场，制造跨
# 不同事件的虚假套利。因此当两市场都已知结算日期且相差超过阈值时，硬否决。
#
# 阈值取 7 天:足以区分「按月份分档」的子市场（相差至少约 30 天），又能容忍两平台
# 对同一截止日的表述差异（如一方记 23:59 ET、另一方记当日 00:00 UTC）。仅当**双方都
# 有**结算日期时才比较——任一方未知则按中性处理（不否决），避免因数据缺失而漏配。
_DATE_MISMATCH_THRESHOLD_DAYS = 7.0


def _date_mismatch(a: CanonicalMarket, b: CanonicalMarket) -> bool:
    """两个市场的结算日期是否相差过大（→ 不同结算窗口 → 不同事件）。

    仅当两市场都有可解析的结算日期、且相差超过 ``_DATE_MISMATCH_THRESHOLD_DAYS`` 天时
    返回 True。任一方日期未知时返回 False（中性，不否决），符合「宁可漏不可错」：日期
    veto 只阻止匹配、不制造匹配，缺数据时不应据此误拒。
    """
    da, db = _close_date(a), _close_date(b)
    if da is None or db is None:
        return False
    # 统一到带时区，避免 naive/aware 相减报错。
    if da.tzinfo is None:
        da = da.replace(tzinfo=timezone.utc)
    if db.tzinfo is None:
        db = db.replace(tzinfo=timezone.utc)
    delta_days = abs((da - db).total_seconds()) / 86400.0
    return delta_days > _DATE_MISMATCH_THRESHOLD_DAYS


def composite_score(
    a: CanonicalMarket,
    b: CanonicalMarket,
    *,
    similarity: SemanticSimilarity,
    weights: Optional[ScoreWeights] = None,
) -> float:
    """Composite cross-platform similarity for two markets in ``[0, 1]``.

    Blends (per design MatchingEngine step 2):

    - title semantic similarity via the ``SemanticSimilarity`` interface,
    - entity overlap of the titles,
    - outcome-structure compatibility (both binary YES/NO), and
    - resolution/close-date proximity.

    Because the weights are normalized to sum to 1.0 and every component is in
    ``[0, 1]``, the result is guaranteed to be in ``[0, 1]`` (Property 7).
    """
    # 赛段/范围闸门（保护 cross_ref 与普通评分两条路径）：若两个标题在「夺冠 vs 赢小组」、
    # 不同组别、不同淘汰赛轮次等赛段限定词上不一致，则它们是不同事件，绝不视为等价。
    # 真实数据中「Will Mexico win the World Cup」(1.4%) 与「Will Mexico win Group A」
    # (56%) 词法相似度高达 0.86，正是被此闸门拦下，避免 122% 的虚假套利。
    if _scope_mismatch(a, b):
        return 0.0

    # 数字/阈值/日期闸门：关键数字不同（如 4 次 vs 7 次降息、$100k vs $110k、
    # 2025 vs 2026）→ 不同问题，绝不视为等价。同样保护 cross_ref 与普通评分两条路径。
    if _number_mismatch(a, b):
        return 0.0

    # 结算日期闸门（Tier-0 红线 C）：两市场都已知结算日期且相差过大（不同结算窗口）→
    # 不同事件。修复「标题相同、endDate 不同」的子市场错配（如外星人事件下按月份分档的
    # 多个子市场）。亦保护 cross_ref 路径：平台自报关联也可能指向不同结算窗口的市场。
    if _date_mismatch(a, b):
        return 0.0

    # 金标准提升（Phase Three）：平台自报的跨平台关联是强信号，但**不盲信**——
    # 真实数据中 cross_ref 可能不精确（例如把 "England win" 错关联到 "Europe win"），
    # 若直接短路返回 1.0 会产生虚假套利。因此即便有 cross_ref，仍要求标题有起码的
    # 相似度作为合理性校验：两者都满足才给接近满分；标题明显不符则大幅下调，避免把
    # 不同事件当成同一事件。
    w = (weights or ScoreWeights()).normalized()
    title = similarity.score(a.title, b.title)
    entities = _entity_overlap(a, b)
    structure = 1.0 if (_is_binary_yes_no(a) and _is_binary_yes_no(b)) else 0.0
    dates = _date_proximity(a, b)

    if _cross_referenced(a, b):
        # cross_ref 提供强先验，但用标题相似度做闸门：标题足够像才认可为高置信度，
        # 否则回退到普通复合评分（不享受金标准加成），从而拒绝不一致的自报关联。
        #
        # TITLE_GATE 选 0.85 的依据（基于两个真实样本实测，LexicalSimilarity）：
        #   - "夺冠" vs "赢小组H"（不同事件，须拦）："Will Spain win the 2026 FIFA
        #     World Cup?" vs "Will Spain win Group H in the 2026 World Cup" = 0.765；
        #   - 真同一事件（须留）："Will Spain win the 2026 FIFA World Cup?" vs
        #     "Spain win the 2026 World Cup" = 0.917。
        # 0.85 落在二者之间，能区分「夺冠/小组赛」错配与「真同一事件」。
        #
        # 诚实说明：0.85 是基于上述两个真实样本选定的**经验阈值**，并非万能解；
        # 不同体育/政治/加密题材的措辞差异很大，后续应收集更多真实错配/正配样本来
        # 校准（甚至按题材分桶设阈值）。当前仅以这两例为锚点，避免盲信 cross_ref。
        TITLE_GATE = 0.85
        if title >= TITLE_GATE:
            # 标题一致：给接近满分（融合 cross_ref 先验与标题证据）。
            return max(0.0, min(1.0, 0.5 + 0.5 * title))
        # 标题明显不符：不信任该 cross_ref，落回普通评分。

    score = (
        w.title * title
        + w.entity_overlap * entities
        + w.structure * structure
        + w.date_proximity * dates
    )
    # Clamp defensively against floating-point drift.
    return max(0.0, min(1.0, score))


def map_outcomes(
    members: List[CanonicalMarket],
) -> Optional[List[OutcomeAlignment]]:
    """Align outcomes across binary YES/NO ``members`` (Req 4.3, 4.6).

    Produces one :class:`OutcomeAlignment` per canonical outcome (``"YES"`` and
    ``"NO"``). For each member, the YES-polarity outcome maps to canonical YES
    and the NO-polarity outcome to canonical NO. A platform whose outcomes
    invert the canonical phrasing is recorded with ``inverted=True`` for the
    relevant alignment.

    Returns ``None`` when *any* member has an outcome whose polarity cannot be
    determined or lacks a clean YES/NO pair — signalling that the market must be
    excluded from the group because not every outcome maps (Req 4.6, Property 6).
    """
    if not members:
        return None

    yes_alignment = OutcomeAlignment(canonical_outcome="YES")
    no_alignment = OutcomeAlignment(canonical_outcome="NO")

    for market in members:
        if not _is_binary_yes_no(market):
            # Non-binary or ambiguous structure: cannot map every outcome.
            return None

        yes_outcome: Optional[Outcome] = None
        no_outcome: Optional[Outcome] = None
        for outcome in market.outcomes:
            polarity = _outcome_polarity(outcome.name)
            if polarity is True:
                yes_outcome = outcome
            elif polarity is False:
                no_outcome = outcome

        # Every outcome of the market must be mapped (Req 4.6).
        if yes_outcome is None or no_outcome is None:
            return None

        # Determine whether this platform phrases the question as the negation.
        # When the platform's own "YES" outcome name does not look affirmative
        # we treat the canonical YES as inverted relative to it. With strict
        # YES/NO polarity classification the direct mapping is non-inverted;
        # inversion is recorded per-platform so downstream pricing can flip it.
        inverted = _market_is_inverted(market)

        yes_alignment.platform_outcomes[market.platform] = (
            no_outcome.name if inverted else yes_outcome.name
        )
        yes_alignment.inverted[market.platform] = inverted

        no_alignment.platform_outcomes[market.platform] = (
            yes_outcome.name if inverted else no_outcome.name
        )
        no_alignment.inverted[market.platform] = inverted

    return [yes_alignment, no_alignment]


def _market_is_inverted(market: CanonicalMarket) -> bool:
    """Whether ``market`` phrases its binary question as the negation.

    A market is considered inverted when its title is framed as a negation
    (contains an explicit "not"/"won't"/"fail" style cue). This lets the engine
    align a "Will X happen?" market against a "Will X NOT happen?" market by
    swapping YES/NO (Req 4.3).
    """
    negation_cues = {"not", "wont", "won", "fail", "fails", "never", "without"}
    tokens = set(tokenize(market.title))
    return bool(tokens & negation_cues)


@dataclass
class MatchingEngine:
    """Ties blocking + scoring + outcome mapping + grouping together (Req 4.1-4.6).

    The engine is deterministic given its inputs and configuration: it blocks to
    generate cross-platform candidate pairs, scores each pair, keeps pairs whose
    composite score meets ``score_threshold``, maps their outcomes (excluding any
    market whose outcomes cannot all be mapped, Req 4.6 / Property 6), and emits
    one :class:`EquivalentMarketGroup` per matched pair with a ``match_confidence``
    in ``[0, 1]`` (Req 4.4 / Property 7).
    """

    similarity: SemanticSimilarity = field(default_factory=LexicalSimilarity)
    blocker: Blocker = field(default_factory=Blocker)
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    # Minimum composite score for a candidate pair to be considered a match.
    score_threshold: float = 0.5

    def score_pairs(self, markets: List[CanonicalMarket]) -> List[ScoredPair]:
        """Score every blocked candidate pair, deterministically ordered."""
        pairs = self.blocker.candidate_pairs(markets)
        scored = [
            ScoredPair(
                pair=pair,
                score=composite_score(
                    pair.a,
                    pair.b,
                    similarity=self.similarity,
                    weights=self.weights,
                ),
            )
            for pair in pairs
        ]
        # Deterministic order: by descending score, then by canonical pair key.
        scored.sort(key=lambda s: (-s.score, s.pair.key))
        return scored

    def match(self, markets: List[CanonicalMarket]) -> List[EquivalentMarketGroup]:
        """Group equivalent cross-platform markets (Req 4.2-4.6).

        Returns groups ordered by descending ``match_confidence`` then group id,
        so the output is stable regardless of input ordering.
        """
        groups: List[EquivalentMarketGroup] = []

        for scored in self.score_pairs(markets):
            if scored.score < self.score_threshold:
                continue

            members = [scored.pair.a, scored.pair.b]
            outcome_map = map_outcomes(members)
            # Req 4.6 / Property 6: if any outcome cannot be mapped, the market
            # is excluded — which for a two-member candidate dissolves the group.
            if outcome_map is None:
                continue

            group_id = self._group_id(members)
            groups.append(
                EquivalentMarketGroup(
                    group_id=group_id,
                    members=members,
                    outcome_map=outcome_map,
                    match_confidence=scored.score,
                )
            )

        groups.sort(key=lambda g: (-g.match_confidence, g.group_id))
        return groups

    @staticmethod
    def _group_id(members: List[CanonicalMarket]) -> str:
        """Deterministic group identifier from sorted member keys."""
        keys = sorted(f"{m.platform}:{m.market_id}" for m in members)
        return "|".join(keys)


__all__ = [
    "STOPWORDS",
    "normalize_text",
    "tokenize",
    "extract_entities",
    "SemanticSimilarity",
    "LexicalSimilarity",
    "EmbeddingSimilarity",
    "HybridSimilarity",
    "NormalizingLexicalSimilarity",
    "CandidatePair",
    "Blocker",
    "KeyExtractor",
    "all_cross_platform_pairs",
    "ScoreWeights",
    "ScoredPair",
    "composite_score",
    "map_outcomes",
    "MatchingEngine",
]
