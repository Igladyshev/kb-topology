"""Runs validation, fix proposals and the retirement report once per snapshot."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .fixes import PROBLEMS, FixProposal, propose_fixes
from .model import Snapshot
from .retirement import RetirementItem, retirement_report
from .validation import DEFAULT_WEIGHTS, EdgeValidation, validate_edges


@dataclass(slots=True)
class Analysis:
    snapshot: Snapshot
    weights: dict[str, float]
    validations: list[EdgeValidation]  # same order as snapshot.edges
    fixes: list[FixProposal]
    retirement: list[RetirementItem]
    validation_by_edge: dict[tuple[str, str], EdgeValidation] = field(default_factory=dict)
    fixes_by_edge: dict[tuple[str, str], list[FixProposal]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "kb_hash": self.snapshot.kb_hash,
            "env": self.snapshot.env,
            "edges": len(self.validations),
            "validation_status": dict(Counter(v.validation_status for v in self.validations)),
            "signals": dict(Counter(k for v in self.validations for k in v.scores)),
            "fix_problems": dict(Counter(f.problem for f in self.fixes)),
            "fix_actions": {
                f"{p}/{a}": n
                for (p, a), n in Counter((f.problem, f.recommended_action) for f in self.fixes).items()
            },
            "retirement": dict(Counter(i.category for i in self.retirement)),
        }


def analyze(snap: Snapshot, now: datetime | None = None, weights: dict[str, float] | None = None) -> Analysis:
    validations = validate_edges(snap, now, weights)
    # Record the validator's confidence on each edge (null until scored).
    snap.edges[:] = [replace(e, confidence=v.confidence) for e, v in zip(snap.edges, validations)]
    fixes = propose_fixes(snap)
    order = {p: i for i, p in enumerate(PROBLEMS)}
    fixes.sort(key=lambda f: (order[f.problem], -f.confidence, f.source_id, f.target_id))
    analysis = Analysis(snap, weights or dict(DEFAULT_WEIGHTS), validations, fixes, retirement_report(snap))
    analysis.validation_by_edge = {(v.source_id, v.target_id): v for v in validations}
    for f in fixes:
        analysis.fixes_by_edge.setdefault((f.source_id, f.target_id), []).append(f)
    return analysis
