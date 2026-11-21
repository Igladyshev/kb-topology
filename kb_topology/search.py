"""Keyword search (design doc, section 5.4): BM25 over node metadata.

Field weights are applied to term counts before BM25 (a simple BM25F). Body text is not
indexed here. The semantic layer (design 5.5) adds it later.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .model import Node, Snapshot

K1 = 1.2
B = 0.75
PREFIX_MIN_LEN = 3
PREFIX_WEIGHT = 0.5
EXACT_TITLE_BONUS = 5.0

FIELD_WEIGHTS = {"title": 3.0, "tags": 2.0, "type": 1.5, "path": 1.0, "resource": 1.0, "description": 1.0}

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(_CAMEL.sub(" ", text).lower())


@dataclass(slots=True)
class SearchHit:
    node_id: str
    score: float
    matched_terms: list[str]
    copies: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"node_id": self.node_id, "score": round(self.score, 4), "matched_terms": self.matched_terms}
        if self.copies is not None:
            data["copies"] = self.copies
        return data


def status_factor(status_weights: dict[str, float]):
    """Return a score factor that down-ranks nodes by declared status."""
    def factor(node: Node) -> float:
        return float(status_weights.get(node.status or "", 1.0))
    return factor


def _fields(n: Node) -> dict[str, str]:
    return {
        "title": n.title,
        "tags": " ".join(n.tags),
        "type": n.type,
        "path": n.path.removesuffix(".md"),
        "resource": str(n.metadata.get("resource", "")),
        "description": n.description or "",
    }


class SearchIndex:
    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap
        self.tf: dict[str, dict[str, float]] = defaultdict(dict)  # term -> node id -> weighted tf
        self.length: dict[str, float] = {}
        self.titles: dict[str, str] = {}
        for nid, n in snap.nodes.items():
            total = 0.0
            counts: Counter[str] = Counter()
            for name, text in _fields(n).items():
                w = FIELD_WEIGHTS[name]
                for tok in tokenize(text):
                    counts[tok] += w
                    total += w
            for tok, c in counts.items():
                self.tf[tok][nid] = c
            self.length[nid] = total
            self.titles[nid] = " ".join(tokenize(n.title))
        self.n_docs = len(snap.nodes)
        self.avg_len = (sum(self.length.values()) / self.n_docs) if self.n_docs else 0.0
        self.vocab = sorted(self.tf)

    def _idf(self, term: str) -> float:
        df = len(self.tf[term])
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def _expand(self, term: str) -> list[tuple[str, float]]:
        """The term itself if indexed, plus longer indexed terms that start with it."""
        out: list[tuple[str, float]] = []
        if term in self.tf:
            out.append((term, 1.0))
        if len(term) >= PREFIX_MIN_LEN:
            i = bisect_left(self.vocab, term)
            while i < len(self.vocab) and self.vocab[i].startswith(term):
                if self.vocab[i] != term:
                    out.append((self.vocab[i], PREFIX_WEIGHT))
                i += 1
        return out

    def search(
        self,
        query: str,
        node_ok=None,
        limit: int = 20,
        score_factor=None,
        collapse: bool = False,
    ) -> tuple[int, list[SearchHit]]:
        """Return (total matches, top ``limit`` hits)."""
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return 0, []
        scores: dict[str, float] = defaultdict(float)
        matched: dict[str, set[str]] = defaultdict(set)
        for term in terms:
            for indexed, weight in self._expand(term):
                idf = self._idf(indexed)
                for nid, tf in self.tf[indexed].items():
                    norm = tf + K1 * (1 - B + B * self.length[nid] / self.avg_len)
                    scores[nid] += weight * idf * tf * (K1 + 1) / norm
                    matched[nid].add(term)
        wanted = " ".join(terms)
        hits: list[SearchHit] = []
        for nid, score in scores.items():
            node = self.snap.nodes[nid]
            if node_ok is not None and not node_ok(node):
                continue
            if self.titles[nid] == wanted:
                score += EXACT_TITLE_BONUS
            if score_factor is not None:
                score *= score_factor(node)
            hits.append(SearchHit(nid, score, sorted(matched[nid])))
        hits.sort(key=lambda h: (-h.score, h.node_id))
        if collapse:
            grouped: dict[str, list[SearchHit]] = {}
            for hit in hits:
                family = self.snap.family_of(hit.node_id)
                grouped.setdefault(family, []).append(hit)
            collapsed: list[SearchHit] = []
            for family, copies in grouped.items():
                best = copies[0]
                ids = sorted({hit.node_id for hit in copies})
                collapsed.append(SearchHit(best.node_id, best.score, best.matched_terms, ids))
            collapsed.sort(key=lambda h: (-h.score, h.node_id))
            hits = collapsed
        return len(hits), hits[:limit]
