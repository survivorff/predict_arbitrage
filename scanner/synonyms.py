"""词条归一：同义词 / 缩写映射（Phase Two · 切片 D 信号质量）。

跨平台的等价市场常用不同措辞描述同一事件：一个平台写 "POTUS"、另一个写
"president"；一个写 "BTC"、另一个写 "bitcoin"。纯词法相似度会因此漏配，导致**漏信号**。

本模块提供一个轻量、确定、零外部依赖的词条归一表，把同义/缩写词映射到同一规范词，
供 :class:`~scanner.matching.NormalizingLexicalSimilarity` 在分词后归一，从而提升
等价市场召回。它是「增强词法」与「真正 embedding 语义后端」之间的务实折中——后者可
随时通过实现 ``SemanticSimilarity`` 接口接入。

词典刻意保持小而高置信：只收录歧义低、跨平台高频的金融/政治/体育术语，避免引入误配。
运营者可通过构造参数扩展或替换词典。
"""

from __future__ import annotations

from typing import Dict, Iterable, List

# 规范词 -> 该规范词的同义/缩写变体集合。
# 归一时把任一变体（含规范词本身）映射到规范词。
DEFAULT_SYNONYM_GROUPS: Dict[str, List[str]] = {
    # 加密货币
    "bitcoin": ["btc", "xbt"],
    "ethereum": ["eth"],
    # 政治
    "president": ["potus", "presidential"],
    "democrat": ["democratic", "dem", "dems"],
    "republican": ["gop", "republicans"],
    "election": ["elections", "electoral"],
    "nominee": ["nomination", "nominees"],
    # 通用比较 / 阈值措辞
    "above": ["over", "exceed", "exceeds", "surpass", "surpasses", "greater"],
    "below": ["under", "beneath", "less", "lower"],
    "win": ["wins", "winner", "victory", "victorious"],
    # 体育
    "championship": ["champions", "champion", "title"],
    "superbowl": ["superbowls"],
}


def _build_lookup(groups: Dict[str, List[str]]) -> Dict[str, str]:
    """把「规范词 -> 变体列表」展开成「任一词 -> 规范词」的查表。"""
    lookup: Dict[str, str] = {}
    for canonical, variants in groups.items():
        lookup[canonical] = canonical
        for variant in variants:
            lookup[variant] = canonical
    return lookup


class SynonymNormalizer:
    """把词条归一到规范词的轻量归一器。

    给定一个「规范词 -> 变体」词典，:meth:`normalize` 将单个词条映射到其规范词
    （未收录的词原样返回）。确定、无副作用、零外部依赖。
    """

    def __init__(self, groups: Dict[str, List[str]] = None) -> None:
        self._lookup = _build_lookup(groups if groups is not None else DEFAULT_SYNONYM_GROUPS)

    def normalize(self, token: str) -> str:
        return self._lookup.get(token, token)

    def normalize_all(self, tokens: Iterable[str]) -> List[str]:
        return [self.normalize(t) for t in tokens]


__all__ = ["DEFAULT_SYNONYM_GROUPS", "SynonymNormalizer"]
