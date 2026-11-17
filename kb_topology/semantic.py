"""Semantic search with topology-aware highlighting (design doc, section 5.5).

For a query:
1. Embed the query and every node text with the same local model.
2. Score candidates by cosine similarity (``semantic_score``), blended with the normalized
   keyword score (``keyword_score``) into ``match_score``. Names such as table identifiers
   match better by keyword than by embedding, so the blend is the default.
3. Expand from the best matches through the graph. A node ``v`` at graph distance ``d`` from
   match ``m`` gets ``match_score(m) * exp(-alpha * d)``. Its ``highlight_score`` is the
   maximum over all matches.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .model import Node, Snapshot
from .navigation import Navigator
from .search import SearchIndex

if TYPE_CHECKING:  # numpy and the embedding classes are only needed when semantic search runs
    import numpy as np

    from .embeddings import EmbeddingProvider, EmbeddingStore

DEFAULT_ALPHA = 0.7
DEFAULT_HOPS = 2
MAX_HOPS = 3
DEFAULT_MIN_HIGHLIGHT = 0.05
DEFAULT_KEYWORD_WEIGHT = 0.3
DEFAULT_HUB_DEGREE = 300  # expansion reaches a hub but does not continue through it
MAX_EXPANDED = 500
DEFAULT_MAX_EXPANDED = 100

_COLUMN_RE = re.compile(r"^\|\s*`([A-Za-z0-9_$ .-]{1,80})`\s*\|", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
_FOOTNOTE_RE = re.compile(r"\[\^[^\]]*\]")
_GENERIC_HEADINGS = frozenset({"properties", "description", "schema", "overview", "references", "sources"})
MAX_COLUMNS = 30
MAX_HEADINGS = 10


def body_hints(text: str) -> tuple[list[str], list[str]]:
    """Column names (from tables with backticked first cells) and section headings of a concept body."""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        body = text[end + 4 :] if end != -1 else text
    columns = list(dict.fromkeys(_COLUMN_RE.findall(body)))[:MAX_COLUMNS]
    headings = []
    for h in _HEADING_RE.findall(body):
        h = _FOOTNOTE_RE.sub("", h).strip()
        if h and h.lower() not in _GENERIC_HEADINGS and h not in headings:
            headings.append(h)
    return columns, headings[:MAX_HEADINGS]


def _words(text: str) -> str:
    return re.sub(r"[_./-]+", " ", text).strip()


def embedding_text(node: Node, root: Path | None) -> str:
    """Text embedded for a node: identity first (the model truncates long input), then detail."""
    parts = [node.title, node.type]
    if node.tags:
        parts.append(" ".join(_words(t.replace(":", " ")) for t in node.tags))
    if node.description:
        parts.append(node.description)
    parts.append(_words(node.path.removesuffix(".md")))
    if root is not None:
        try:
            columns, headings = body_hints((root / node.path.lstrip("/")).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            columns, headings = [], []
        if columns:
            parts.append("columns: " + ", ".join(columns))
        if headings:
            parts.append("sections: " + ", ".join(headings))
    return ". ".join(p for p in parts if p)


@dataclass(slots=True)
class SemanticHit:
    node_id: str
    semantic_score: float  # cosine similarity between query and node (0 for expanded nodes)
    keyword_score: float  # keyword score normalized to 0..1 against the best keyword hit
    match_score: float  # blend used for ranking and as the seed for highlighting
    hop_distance: int
    highlight_score: float
    match_type: str  # direct | expanded
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "semantic_score": round(self.semantic_score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "match_score": round(self.match_score, 4),
            "hop_distance": self.hop_distance,
            "highlight_score": round(self.highlight_score, 4),
            "match_type": self.match_type,
            "evidence": self.evidence,
        }


class SemanticService:
    """Builds node vectors in a background thread and answers semantic queries."""

    def __init__(
        self,
        snapshot: Snapshot,
        provider: EmbeddingProvider,
        store: EmbeddingStore,
        navigator: Navigator,
        keyword_index: SearchIndex,
    ) -> None:
        self.snap = snapshot
        self.provider = provider
        self.store = store
        self.nav = navigator
        self.keywords = keyword_index
        self.node_ids = list(snapshot.nodes)
        self.matrix: np.ndarray | None = None
        self.state = "pending"  # pending | building | ready | failed
        self.done = 0
        self.total = len(self.node_ids)
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "done": self.done, "total": self.total, "error": self.error, "model": self.provider.model_id}

    def build(self, progress: Callable[[int, int], None] | None = None) -> None:
        self.state = "building"
        try:
            texts = [embedding_text(self.snap.nodes[nid], self.snap.root) for nid in self.node_ids]

            def report(done: int, total: int) -> None:
                self.done = done
                if progress:
                    progress(done, total)

            self.matrix = self.store.matrix(self.provider, texts, report)
            self.done = self.total
            self.state = "ready"
        except Exception as exc:  # surfaced through status(), not raised into the server thread
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "failed"

    def start(self) -> None:
        if self._thread is None and self.state == "pending":
            self.state = "building"
            self._thread = threading.Thread(target=self.build, name="semantic-build", daemon=True)
            self._thread.start()

    # ---- query -----------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 10,
        expand_hops: int = DEFAULT_HOPS,
        alpha: float = DEFAULT_ALPHA,
        keyword_weight: float = DEFAULT_KEYWORD_WEIGHT,
        min_highlight: float = DEFAULT_MIN_HIGHLIGHT,
        max_via_degree: int | None = DEFAULT_HUB_DEGREE,
        node_ok: Callable[[Node], bool] | None = None,
        max_expanded: int = DEFAULT_MAX_EXPANDED,
    ) -> list[SemanticHit]:
        if self.state != "ready" or self.matrix is None:
            raise RuntimeError("semantic index is not ready")
        import numpy as np

        qvec = self.provider.embed([query])[0]
        cosine = self.matrix @ qvec

        pool = max(top_k * 5, 50)
        order = np.argpartition(-cosine, min(pool, len(cosine) - 1))[:pool] if len(cosine) else []
        candidates = {self.node_ids[i]: float(cosine[i]) for i in order}
        _, kw_hits = self.keywords.search(query, node_ok=None, limit=pool)
        best_kw = kw_hits[0].score if kw_hits else 0.0
        kw_norm = {h.node_id: (h.score / best_kw if best_kw > 0 else 0.0) for h in kw_hits}
        index_of = {nid: i for i, nid in enumerate(self.node_ids)}
        for nid in kw_norm:
            candidates.setdefault(nid, float(cosine[index_of[nid]]))

        direct: list[SemanticHit] = []
        for nid, cos in candidates.items():
            if node_ok is not None and not node_ok(self.snap.nodes[nid]):
                continue
            kw = kw_norm.get(nid, 0.0)
            match = (1 - keyword_weight) * max(cos, 0.0) + keyword_weight * kw
            direct.append(SemanticHit(nid, cos, kw, match, 0, match, "direct", {"matched_from": nid}))
        direct.sort(key=lambda h: (-h.match_score, h.node_id))
        direct = direct[:top_k]

        expanded = self._expand(direct, expand_hops, alpha, max_via_degree, node_ok)
        return direct + [h for h in expanded if h.highlight_score >= min_highlight][:max_expanded]

    def _expand(
        self,
        direct: list[SemanticHit],
        hops: int,
        alpha: float,
        max_via_degree: int | None,
        node_ok: Callable[[Node], bool] | None,
    ) -> list[SemanticHit]:
        direct_ids = {h.node_id for h in direct}
        best: dict[str, SemanticHit] = {}
        for m in direct:
            seen = {m.node_id}
            frontier = [m.node_id]
            for d in range(1, hops + 1):
                weight = m.match_score * math.exp(-alpha * d)
                nxt: list[str] = []
                for cur in frontier:
                    for other, _ in self.nav.steps(cur, "both"):
                        if other in seen:
                            continue
                        seen.add(other)
                        if other not in direct_ids and (node_ok is None or node_ok(self.snap.nodes[other])):
                            cur_best = best.get(other)
                            if cur_best is None or weight > cur_best.highlight_score:
                                best[other] = SemanticHit(
                                    other, 0.0, 0.0, 0.0, d, weight, "expanded", {"matched_from": m.node_id}
                                )
                        if max_via_degree is None or self.nav.degree[other] <= max_via_degree:
                            nxt.append(other)
                frontier = nxt
        ranked = sorted(best.values(), key=lambda h: (-h.highlight_score, h.hop_distance, h.node_id))
        return ranked[:MAX_EXPANDED]
