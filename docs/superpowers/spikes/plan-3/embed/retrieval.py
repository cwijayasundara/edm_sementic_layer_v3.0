"""Hybrid retrieval: BM25 (rank_bm25) + dense cosine, fused by RRF or weighted score fusion."""
from __future__ import annotations

import re

import numpy as np
from py_rust_stemmers import SnowballStemmer
from rank_bm25 import BM25Okapi

from corpus import Doc
from embedder import Embedder

_TOKEN = re.compile(r"[a-z0-9]+")
_STEM = SnowballStemmer("english")
STOP = frozenset("a an and are as at be by did do does for from how i in is it many me of on or our "
                 "show the their them there this to was we what which who with".split())


def tokenize(text: str) -> list[str]:
    toks = _TOKEN.findall((text or "").lower().replace("_", " "))
    return [_STEM.stem_word(t) for t in toks if t not in STOP]


class HybridRetriever:
    def __init__(self, docs: list[Doc], embedder: Embedder | None = None,
                 doc_vectors: np.ndarray | None = None) -> None:
        self.docs = docs
        self.ids = [d.id for d in docs]
        self._bm25 = BM25Okapi([tokenize(d.text()) for d in docs])
        self.embedder = embedder
        if embedder is not None and doc_vectors is None:
            doc_vectors = embedder.embed_documents([d.text() for d in docs])
        self.doc_vectors = doc_vectors

    # --- raw scores (len == len(docs)) ---
    def bm25_scores(self, q: str) -> np.ndarray:
        toks = tokenize(q)
        return np.asarray(self._bm25.get_scores(toks)) if toks else np.zeros(len(self.docs))

    def dense_scores(self, q: str) -> np.ndarray:
        return self.doc_vectors @ self.embedder.embed_query(q)

    # --- rankings (list of doc ids, best first) ---
    def _rank(self, scores: np.ndarray) -> list[str]:
        order = np.argsort(-scores, kind="stable")
        return [self.ids[i] for i in order]

    def bm25(self, q: str) -> list[str]:
        return self._rank(self.bm25_scores(q))

    def dense(self, q: str) -> list[str]:
        return self._rank(self.dense_scores(q))

    def rrf(self, q: str, k: int = 60, depth: int = 50) -> list[str]:
        fused: dict[str, float] = {}
        bm = self.bm25_scores(q)
        lists = [self.dense(q)[:depth]]
        pos = [i for i in np.argsort(-bm, kind="stable") if bm[i] > 0][:depth]
        if pos:                                  # only docs with a real lexical match get BM25 credit
            lists.append([self.ids[i] for i in pos])
        for ranking in lists:
            for r, doc_id in enumerate(ranking):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + r + 1)
        return sorted(fused, key=lambda d: -fused[d])

    def weighted(self, q: str, alpha: float = 0.5) -> list[str]:
        """alpha * minmax(dense) + (1 - alpha) * minmax(bm25), both normalised per query."""
        return self._rank(alpha * _minmax(self.dense_scores(q)) + (1 - alpha) * _minmax(self.bm25_scores(q)))


def _minmax(x: np.ndarray) -> np.ndarray:
    rng = x.max() - x.min()
    return (x - x.min()) / rng if rng > 0 else np.zeros_like(x)
