"""Pure-Python BM25 retrieval + RRF fusion (no new dependencies).

Tokenizer: latin word tokens + CJK bigrams (works for zh/en mixed text).
Used as the deterministic track of the hybrid retriever; the vector track
lives in tools/storage/vectorstore.py. RRF(k=60) fuses both ranked lists.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list[str]:
    """Latin lowercase word tokens + CJK bigrams (single CJK char kept if alone)."""
    text = (text or "").lower()
    tokens = _WORD_RE.findall(text)
    cjk = "".join(_CJK_RE.findall(text))
    tokens.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    if len(cjk) == 1:
        tokens.append(cjk)
    return tokens


@dataclass
class BM25Doc:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    """Minimal Okapi BM25 over an in-memory corpus."""

    def __init__(self, docs: list[BM25Doc], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._len: list[int] = []
        for d in docs:
            tf = Counter(tokenize(d.text))
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            for term in tf:
                self._df[term] += 1
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 1.0
        self._n = len(docs)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 5) -> list[tuple[BM25Doc, float]]:
        """Return [(doc, score)] sorted desc; empty query -> []."""
        terms = tokenize(query)
        if not terms or not self.docs:
            return []
        scores: list[tuple[int, float]] = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            doc_len = self._len[i] or 1
            for t in terms:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = self._idf(t)
                denom = f + self.k1 * (1 - self.b + self.b * doc_len / self._avg_len)
                score += idf * f * (self.k1 + 1) / denom
            if score > 0:
                scores.append((i, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(self.docs[i], s) for i, s in scores[:max(top_k, 1)]]


def rrf_merge(ranked_lists: list[list[str]], k: int = 60, top_n: int | None = None) -> list[str]:
    """Reciprocal Rank Fusion over several ranked id lists.

    Each list is ordered best-first. Score(id) = sum(1 / (k + rank)).
    Returns fused ids ordered by fused score desc.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores, key=lambda d: scores[d], reverse=True)
    return fused[:top_n] if top_n else fused
