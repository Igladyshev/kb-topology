"""Fix proposals (design doc, section 9.2).

A proposal says what to change in the KB. The tool never applies it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .model import Edge, Node, Snapshot
from .validation import titles_match

PROPOSE_MIN_CONFIDENCE = 0.75
MAX_CANDIDATES = 5
FUZZY_CUTOFF = 0.85

PROBLEMS = ("broken-target", "bundle-mismatch", "title-mismatch", "body-only")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _squash(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


@dataclass(slots=True)
class Candidate:
    id: str  # node id
    path: str
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "score": self.score, "reason": self.reason}


@dataclass(slots=True)
class FixProposal:
    source_id: str
    target_id: str
    declared_in: tuple[str, ...]
    location: str  # "references[3]" or "body"
    problem: str
    current: dict[str, str | None] | None
    proposed: dict[str, str | None] | None  # ready-to-paste references[] entry; None = no automatic fix
    candidates: list[Candidate] = field(default_factory=list)
    confidence: float = 0.0
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge": {"source_id": self.source_id, "target_id": self.target_id, "declared_in": list(self.declared_in)},
            "location": self.location,
            "problem": self.problem,
            "current": self.current,
            "proposed": self.proposed,
            "candidates": [c.to_dict() for c in self.candidates],
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
        }


def reference_entry(node: Node) -> dict[str, str | None]:
    """A references[] entry as OKF writes it: bundle, id (KB path), title."""
    return {"bundle": node.bundle, "id": node.path, "title": node.title}


class CandidateIndex:
    """Lookups used to find the intended target of a broken link."""

    def __init__(self, nodes: dict[str, Node]) -> None:
        self.by_lower_path: dict[str, list[Node]] = {}
        self.by_lower_stem: dict[str, list[Node]] = {}
        self.by_squashed: dict[str, list[Node]] = {}
        for n in nodes.values():
            self.by_lower_path.setdefault(n.path.lower(), []).append(n)
            self.by_lower_stem.setdefault(_stem(n.path).lower(), []).append(n)
            self.by_squashed.setdefault(_squash(_stem(n.path)), []).append(n)
        self._squashed_keys = list(self.by_squashed)

    def find(self, missing_path: str) -> list[Candidate]:
        found: dict[str, Candidate] = {}
        bundle = missing_path.split("/")[1] if missing_path.count("/") > 1 else ""

        def put(node: Node, score: float, reason: str) -> None:
            if node.path == missing_path:
                return
            if node.bundle == bundle:
                score = min(score + 0.03, 0.99)
            score = round(score, 2)
            cur = found.get(node.id)
            if cur is None or score > cur.score:
                found[node.id] = Candidate(node.id, node.path, score, reason)

        for n in self.by_lower_path.get(missing_path.lower(), ()):
            put(n, 0.95, "same path ignoring case")
        stem = _stem(missing_path).lower()
        same_stem = self.by_lower_stem.get(stem, ())
        for n in same_stem:
            put(n, 0.85 if len(same_stem) == 1 else 0.7, "same file name elsewhere" if len(same_stem) == 1 else "same file name, several matches")
        key = _squash(_stem(missing_path))
        for n in self.by_squashed.get(key, ()):
            put(n, 0.8, "same name ignoring punctuation and case")
        if not found:
            for close in difflib.get_close_matches(key, self._squashed_keys, n=MAX_CANDIDATES, cutoff=FUZZY_CUTOFF):
                ratio = difflib.SequenceMatcher(None, key, close).ratio()
                for n in self.by_squashed[close]:
                    put(n, ratio * 0.75, f"similar name ({ratio:.2f})")
        return sorted(found.values(), key=lambda c: (-c.score, c.path))[:MAX_CANDIDATES]


def propose_fixes(snap: Snapshot) -> list[FixProposal]:
    index = CandidateIndex(snap.nodes)
    cache: dict[str, list[Candidate]] = {}
    out: list[FixProposal] = []
    for e in snap.edges:
        loc = f"references[{e.declared_ref_index}]" if e.declared_ref_index is not None else "body"
        if not e.resolved:
            out.append(_broken(e, snap, index, cache, loc))
            continue
        tgt = snap.nodes[e.target_id]
        in_frontmatter = "frontmatter" in e.declared_in
        current = {"bundle": e.declared_bundle, "id": tgt.path, "title": e.declared_title}
        if in_frontmatter and e.declared_bundle is not None and e.declared_bundle != tgt.bundle:
            out.append(_edit(e, loc, "bundle-mismatch", current, tgt, "update-bundle"))
        if in_frontmatter and e.declared_title is not None and not titles_match(e.declared_title, tgt.title):
            out.append(_edit(e, loc, "title-mismatch", current, tgt, "update-title"))
        if not in_frontmatter:
            out.append(
                FixProposal(
                    e.source_id, e.target_id, e.declared_in, "body", "body-only", None,
                    reference_entry(tgt), [], 0.9, "promote-to-frontmatter",
                )
            )
    return out


def _edit(e: Edge, loc: str, problem: str, current: dict[str, str | None], tgt: Node, action: str) -> FixProposal:
    return FixProposal(e.source_id, e.target_id, e.declared_in, loc, problem, current, reference_entry(tgt), [], 0.9, action)


def _broken(e: Edge, snap: Snapshot, index: CandidateIndex, cache: dict[str, list[Candidate]], loc: str) -> FixProposal:
    missing_path = e.target_id.split(":", 1)[1]
    if missing_path not in cache:
        cache[missing_path] = index.find(missing_path)
    cands = cache[missing_path]
    current = {"bundle": e.declared_bundle, "id": missing_path, "title": e.declared_title}
    top = cands[0] if cands else None
    if top and top.score >= PROPOSE_MIN_CONFIDENCE and (len(cands) == 1 or cands[1].score < top.score):
        proposed = reference_entry(snap.nodes[top.id])
        return FixProposal(e.source_id, e.target_id, e.declared_in, loc, "broken-target", current, proposed, cands, top.score, "retarget")
    return FixProposal(
        e.source_id, e.target_id, e.declared_in, loc, "broken-target", current, None, cands,
        top.score if top else 0.0, "review-candidates" if cands else "remove-or-recreate",
    )
