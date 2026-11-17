"""Edge validation (design doc, section 5.7).

Risk is a noisy-OR of independent signals, each of which scores 0..1 and has a weight.
Weights and thresholds are starting values. The design calls for recalibrating them from
persisted review decisions (section 9), which arrive with the review store.

``semantic_mismatch`` stays null until the embedding provider exists (design 5.5).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import Edge, Node, Snapshot

NEEDS_REVIEW = 0.3
LIKELY_MISLEADING = 0.8

DEFAULT_WEIGHTS = {
    "broken_target": 1.0,
    "bundle_mismatch": 0.4,
    "type_outlier": 0.4,
    "deprecated_target": 0.35,
    "title_mismatch": 0.2,
    "discontinuity": 0.1,
}
WEIGHTS = DEFAULT_WEIGHTS  # kept for callers that read the defaults
OUTLIER_SHARE = 0.01  # a type pair below this share of its source family's edges is an outlier
MIN_DEGREE_FOR_JACCARD = 3

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str | None) -> str:
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


def titles_match(declared: str | None, actual: str | None) -> bool:
    """Equal after normalization, or one is a prefix of the other (e.g. a "— SQL" suffix)."""
    a, b = normalize_title(declared), normalize_title(actual)
    return a == b or a.startswith(b) or b.startswith(a)


@dataclass(slots=True)
class EvidenceItem:
    kind: str
    detail: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "score": self.score}


@dataclass(slots=True)
class EdgeValidation:
    source_id: str
    target_id: str
    validation_status: str  # validated | needs-review | likely-misleading
    risk_score: float
    confidence: float  # 1 - risk_score
    semantic_mismatch: float | None
    scores: dict[str, float]
    evidence: list[EvidenceItem] = field(default_factory=list)
    recommended_action: str = "none"
    validated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "validation_status": self.validation_status,
            "risk_score": self.risk_score,
            "confidence": self.confidence,
            "semantic_mismatch": self.semantic_mismatch,
            "scores": self.scores,
            "evidence": [e.to_dict() for e in self.evidence],
            "recommended_action": self.recommended_action,
            "validated_at": self.validated_at,
        }


_ACTION_PRIORITY = [
    ("broken_target", "retarget-or-remove"),
    ("bundle_mismatch", "update-bundle"),
    ("title_mismatch", "update-title"),
    ("deprecated_target", "review-deprecated-target"),
    ("type_outlier", "review"),
]


def status_for(risk: float) -> str:
    if risk >= LIKELY_MISLEADING:
        return "likely-misleading"
    if risk >= NEEDS_REVIEW:
        return "needs-review"
    return "validated"


def _noisy_or(contributions: list[float]) -> float:
    keep = 1.0
    for c in contributions:
        keep *= 1.0 - c
    return 1.0 - keep


class _Context:
    """Snapshot-wide statistics shared by all edge checks."""

    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap
        self.family_edges: Counter[str] = Counter()
        self.pair_edges: Counter[str] = Counter()
        neighbors: dict[str, set[str]] = defaultdict(set)
        for e in snap.edges:
            if not e.resolved:
                continue
            self.family_edges[snap.nodes[e.source_id].type_family] += 1
            self.pair_edges[e.relation_type or ""] += 1
            neighbors[e.source_id].add(e.target_id)
            neighbors[e.target_id].add(e.source_id)
        self.neighbors = neighbors

    def pair_share(self, e: Edge, src: Node) -> float:
        total = self.family_edges[src.type_family]
        return self.pair_edges[e.relation_type or ""] / total if total else 1.0

    def jaccard(self, e: Edge) -> float | None:
        a = self.neighbors[e.source_id] - {e.target_id}
        b = self.neighbors[e.target_id] - {e.source_id}
        if len(a) < MIN_DEGREE_FOR_JACCARD or len(b) < MIN_DEGREE_FOR_JACCARD:
            return None
        return len(a & b) / len(a | b)


def load_weights(state_dir: Path | str | None) -> dict[str, float]:
    """Default weights, overridden by ``<state_dir>/weights.json`` (written by ``calibrate --apply``)."""
    weights = dict(DEFAULT_WEIGHTS)
    if state_dir is None:
        return weights
    path = Path(state_dir) / "weights.json"
    if path.is_file():
        for name, value in json.loads(path.read_text(encoding="utf-8")).items():
            if name in weights and isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
                weights[name] = float(value)
    return weights


def validate_edges(
    snap: Snapshot, now: datetime | None = None, weights: dict[str, float] | None = None
) -> list[EdgeValidation]:
    """Validate every edge. Result order matches ``snap.edges``."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    ctx = _Context(snap)
    w = weights or DEFAULT_WEIGHTS
    return [_validate(e, snap, ctx, stamp, w) for e in snap.edges]


def _validate(e: Edge, snap: Snapshot, ctx: _Context, stamp: str, weights: dict[str, float]) -> EdgeValidation:
    scores: dict[str, float] = {}
    evidence: list[EvidenceItem] = []

    def add(name: str, score: float, detail: str) -> None:
        scores[name] = score
        evidence.append(EvidenceItem(name, detail, score))

    src = snap.nodes[e.source_id]
    tgt = snap.nodes.get(e.target_id)
    if tgt is None:
        add("broken_target", 1.0, f"target {e.target_id} does not exist in the bundle")
    else:
        if e.declared_bundle is not None and e.declared_bundle != tgt.bundle:
            add("bundle_mismatch", 1.0, f"declared bundle '{e.declared_bundle}', target is in '{tgt.bundle}'")
        if "frontmatter" in e.declared_in and e.declared_title is not None and not titles_match(e.declared_title, tgt.title):
            add("title_mismatch", 1.0, f"declared title '{e.declared_title}', target title is '{tgt.title}'")
        if (tgt.status or "").lower() == "deprecated" and (src.status or "").lower() != "deprecated":
            add("deprecated_target", 1.0, f"target is deprecated, source status is '{src.status}'")
        share = ctx.pair_share(e, src)
        if share < OUTLIER_SHARE:
            add("type_outlier", 1.0, f"'{e.relation_type}' is {share:.2%} of '{src.type_family}' edges")
        j = ctx.jaccard(e)
        if j == 0:
            add("discontinuity", 1.0, "endpoints share no neighbors")

    risk = round(_noisy_or([weights[k] * v for k, v in scores.items()]), 4)
    action = next((a for k, a in _ACTION_PRIORITY if k in scores), "none")
    return EdgeValidation(
        source_id=e.source_id,
        target_id=e.target_id,
        validation_status=status_for(risk),
        risk_score=risk,
        confidence=round(1.0 - risk, 4),
        semantic_mismatch=None,
        scores=scores,
        evidence=evidence,
        recommended_action=action,
        validated_at=stamp,
    )
