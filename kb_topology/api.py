"""HTTP API over loaded snapshots (design doc, section 7)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from .analysis import Analysis, analyze
from .paths import resolve_state_dir
from .fixes import PROBLEMS
from .model import Edge, Snapshot
from .navigation import EdgeCriteria, Navigator, NodeCriteria, edge_to_dict
from .search import SearchIndex
from . import semantic as sem

if TYPE_CHECKING:
    from .embeddings import EmbeddingProvider
from .sessions import SESSION_ID_RE, SessionStore
from .state import DECISIONS, ReviewRecord, ReviewStore, StateDB, calibration
from .validation import load_weights

ValidationStatus = Literal["validated", "needs-review", "likely-misleading"]


class EdgeRef(BaseModel):
    source_id: str
    target_id: str


class ValidateRequest(BaseModel):
    edges: list[EdgeRef] = Field(min_length=1, max_length=1000)


class Correction(BaseModel):
    bundle: str | None = None
    id: str
    title: str | None = None


class ReviewRequest(BaseModel):
    source_id: str
    target_id: str
    decision: Literal["approved", "rejected", "corrected"]
    correction: Correction | None = None
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _correction_required(self) -> "ReviewRequest":
        if self.decision == "corrected" and self.correction is None:
            raise ValueError("a 'corrected' decision needs a correction")
        return self


class VisitRequest(BaseModel):
    kb_hash: str
    node_id: str
    via: str | None = Field(default=None, max_length=64)


def create_app(
    snapshots: list[Snapshot],
    sessions: SessionStore | None = None,
    provider: EmbeddingProvider | None = None,
    state_dir: Path | None = None,
    semantic_sync: bool = False,
) -> FastAPI:
    """Build the API. With a ``provider``, semantic search is enabled and vectors are built in a
    background thread at startup (or immediately when ``semantic_sync`` is true, for tests)."""
    weights = load_weights(state_dir)
    db = StateDB(Path(state_dir) / "state.db" if state_dir else None)  # in memory when no state_dir is given
    reviews = ReviewStore(db)
    analyses: dict[str, Analysis] = {s.kb_hash: analyze(s, weights=weights) for s in snapshots}
    navigators = {h: Navigator(a) for h, a in analyses.items()}
    search_indexes = {s.kb_hash: SearchIndex(s) for s in snapshots}
    by_hash = {s.kb_hash: s for s in snapshots}
    sessions = sessions or SessionStore(db=db)
    semantic: dict[str, sem.SemanticService] = {}
    if provider is not None:
        from .embeddings import EmbeddingStore

        store = EmbeddingStore(resolve_state_dir(state_dir), provider.model_id)
        for snap in snapshots:
            semantic[snap.kb_hash] = sem.SemanticService(
                snap, provider, store, navigators[snap.kb_hash], search_indexes[snap.kb_hash]
            )
        if semantic_sync:
            for svc in semantic.values():
                svc.build()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for svc in semantic.values():
            svc.start()
        yield

    app = FastAPI(title="kb-topology", version="0.2.0", lifespan=lifespan)

    def get_snapshot(kb_hash: str) -> Snapshot:
        snap = by_hash.get(kb_hash)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"unknown kb_hash: {kb_hash}")
        return snap

    def get_nav(kb_hash: str) -> Navigator:
        get_snapshot(kb_hash)
        return navigators[kb_hash]

    def require_node(snap: Snapshot, node_id: str) -> None:
        if node_id not in snap.nodes:
            raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")

    def review_state(snap: Snapshot, rec: ReviewRecord) -> str:
        """fresh while both endpoint files still have the content hashes the reviewer saw."""
        src, tgt = snap.nodes.get(rec.source_id), snap.nodes.get(rec.target_id)
        same = (src.kb_ref.content_hash if src else None) == rec.source_hash and (
            tgt.kb_ref.content_hash if tgt else None
        ) == rec.target_hash
        return "fresh" if same else "stale"

    def review_dict(snap: Snapshot, rec: ReviewRecord | None) -> dict[str, Any] | None:
        return {**rec.to_dict(), "state": review_state(snap, rec)} if rec else None

    def latest_reviews(snap: Snapshot) -> dict[tuple[str, str], ReviewRecord]:
        return reviews.latest(f"{snap.env}:")

    def get_analysis(kb_hash: str) -> Analysis:
        get_snapshot(kb_hash)
        return analyses[kb_hash]

    @app.get("/kb")
    def list_snapshots() -> list[dict[str, Any]]:
        return [s.summary() for s in by_hash.values()]

    @app.get("/kb/{kb_hash}/diagnostics")
    def diagnostics(kb_hash: str) -> dict[str, Any]:
        return get_snapshot(kb_hash).diagnostics.to_dict()

    @app.get("/kb/{kb_hash}/links")
    def links(
        kb_hash: str,
        node: Annotated[str | None, Query(description="Node id, e.g. prod:/area/group/item.md")] = None,
        direction: Literal["out", "in", "both"] = "both",
        relation_alias: str | None = None,
        relation_type: str | None = None,
        declared_in: Literal["frontmatter", "body"] | None = None,
        resolved: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Forward and reverse link maps (each node's outgoing and incoming links), with filters."""
        snap = get_snapshot(kb_hash)
        if node is None:
            candidates: list[Edge] = snap.edges
        else:
            if node not in snap.nodes:
                raise HTTPException(status_code=404, detail=f"unknown node: {node}")
            idx: list[int] = []
            if direction in ("out", "both"):
                idx.extend(snap.forward.get(node, ()))
            if direction in ("in", "both"):
                idx.extend(snap.reverse.get(node, ()))
            candidates = [snap.edges[i] for i in dict.fromkeys(idx)]

        def keep(e: Edge) -> bool:
            return (
                (relation_alias is None or e.relation_alias == relation_alias)
                and (relation_type is None or e.relation_type == relation_type)
                and (declared_in is None or declared_in in e.declared_in)
                and (resolved is None or e.resolved == resolved)
            )

        matched = [e for e in candidates if keep(e)]
        return {
            "kb_hash": snap.kb_hash,
            "total": len(matched),
            "limit": limit,
            "offset": offset,
            "items": [edge_to_dict(e) for e in matched[offset : offset + limit]],
        }

    @app.get("/kb/{kb_hash}/links/validation")
    def link_validation(
        kb_hash: str,
        status: Literal["validated", "needs-review", "likely-misleading"] | None = None,
        min_risk: Annotated[float, Query(ge=0, le=1)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Validation results, highest risk first."""
        a = get_analysis(kb_hash)
        matched = [
            v for v in a.validations
            if (status is None or v.validation_status == status) and v.risk_score >= min_risk
        ]
        matched.sort(key=lambda v: (-v.risk_score, v.source_id, v.target_id))
        decided = latest_reviews(a.snapshot)
        return {
            "kb_hash": kb_hash,
            "total": len(matched),
            "limit": limit,
            "offset": offset,
            "items": [
                {**v.to_dict(), "review": review_dict(a.snapshot, decided.get((v.source_id, v.target_id)))}
                for v in matched[offset : offset + limit]
            ],
        }

    @app.post("/kb/{kb_hash}/links/validate")
    def validate_links(kb_hash: str, request: Annotated[ValidateRequest, Body()]) -> dict[str, Any]:
        """Validation and fix proposals for the given edges."""
        a = get_analysis(kb_hash)
        decided = latest_reviews(a.snapshot)
        items, unknown = [], []
        for ref in request.edges:
            key = (ref.source_id, ref.target_id)
            v = a.validation_by_edge.get(key)
            if v is None:
                unknown.append(ref.model_dump())
                continue
            items.append({
                "validation": v.to_dict(),
                "fixes": [f.to_dict() for f in a.fixes_by_edge.get(key, ())],
                "review": review_dict(a.snapshot, decided.get(key)),
            })
        return {"kb_hash": kb_hash, "items": items, "unknown": unknown}

    @app.post("/kb/{kb_hash}/links/review", status_code=201)
    def review_link(kb_hash: str, request: Annotated[ReviewRequest, Body()]) -> dict[str, Any]:
        """Record a human decision about an edge. The KB is not changed."""
        a = get_analysis(kb_hash)
        snap = a.snapshot
        key = (request.source_id, request.target_id)
        v = a.validation_by_edge.get(key)
        if v is None:
            raise HTTPException(status_code=404, detail="unknown edge")
        tgt = snap.nodes.get(request.target_id)
        rec = reviews.record(
            kb_hash, request.source_id, request.target_id,
            snap.nodes[request.source_id].kb_ref.content_hash, tgt.kb_ref.content_hash if tgt else None,
            request.decision, v.risk_score, list(v.scores),
            request.correction.model_dump() if request.correction else None, request.note, request.reviewer,
        )
        return {"review": review_dict(snap, rec), "validation": v.to_dict()}

    @app.get("/kb/{kb_hash}/review/queue")
    def review_queue(
        kb_hash: str,
        status: Literal["needs-review", "likely-misleading"] | None = None,
        min_risk: Annotated[float, Query(ge=0, le=1)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Flagged edges without a current decision, highest risk first. Stale decisions re-enter the queue."""
        a = get_analysis(kb_hash)
        decided = latest_reviews(a.snapshot)
        queue = []
        for v in a.validations:
            if v.validation_status == "validated" or v.risk_score < min_risk or (status and v.validation_status != status):
                continue
            rec = decided.get((v.source_id, v.target_id))
            if rec is not None and review_state(a.snapshot, rec) == "fresh":
                continue
            queue.append((v, rec))
        queue.sort(key=lambda x: (-x[0].risk_score, x[0].source_id, x[0].target_id))
        return {
            "kb_hash": kb_hash,
            "total": len(queue),
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "validation": v.to_dict(),
                    "fixes": [f.to_dict() for f in a.fixes_by_edge.get((v.source_id, v.target_id), ())],
                    "review": review_dict(a.snapshot, rec),
                }
                for v, rec in queue[offset : offset + limit]
            ],
        }

    @app.get("/kb/{kb_hash}/review/decisions")
    def review_decisions(
        kb_hash: str,
        decision: Literal["approved", "rejected", "corrected"] | None = None,
        state: Literal["fresh", "stale"] | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Current decision per edge, newest first."""
        snap = get_analysis(kb_hash).snapshot
        rows = [review_dict(snap, r) for r in sorted(latest_reviews(snap).values(), key=lambda r: -r.id)]
        rows = [r for r in rows if (decision is None or r["decision"] == decision) and (state is None or r["state"] == state)]
        return {"kb_hash": kb_hash, "total": len(rows), "limit": limit, "offset": offset, "items": rows[offset : offset + limit]}

    @app.get("/kb/{kb_hash}/review/history")
    def review_history(kb_hash: str, source_id: str, target_id: str) -> dict[str, Any]:
        snap = get_analysis(kb_hash).snapshot
        return {"items": [review_dict(snap, r) for r in reviews.history(source_id, target_id)]}

    @app.get("/kb/{kb_hash}/review/calibration")
    def review_calibration(kb_hash: str, min_samples: Annotated[int, Query(ge=1)] = 20) -> dict[str, Any]:
        """Per-signal precision from decisions, with suggested weights. Suggestions are never applied automatically."""
        a = get_analysis(kb_hash)
        return calibration(latest_reviews(a.snapshot), a.weights, min_samples)

    @app.get("/kb/{kb_hash}/fixes")
    def fixes(
        kb_hash: str,
        problem: Literal[PROBLEMS] | None = None,  # type: ignore[valid-type]
        recommended_action: str | None = None,
        source_id: str | None = None,
        min_confidence: Annotated[float, Query(ge=0, le=1)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Fix proposals. The tool never applies them."""
        a = get_analysis(kb_hash)
        matched = [
            f for f in a.fixes
            if (problem is None or f.problem == problem)
            and (recommended_action is None or f.recommended_action == recommended_action)
            and (source_id is None or f.source_id == source_id)
            and f.confidence >= min_confidence
        ]
        return {
            "kb_hash": kb_hash,
            "total": len(matched),
            "limit": limit,
            "offset": offset,
            "items": [f.to_dict() for f in matched[offset : offset + limit]],
        }

    @app.get("/kb/{kb_hash}/retirement")
    def retirement(
        kb_hash: str, category: Literal["retirement-candidate", "isolated-review"] | None = None
    ) -> dict[str, Any]:
        """Advice for the KB owners. Nothing is deleted."""
        items = [i.to_dict() for i in get_analysis(kb_hash).retirement if category is None or i.category == category]
        return {"kb_hash": kb_hash, "total": len(items), "items": items}

    @app.get("/kb/{kb_hash}/node/{node_id:path}/neighbors")
    def node_neighbors(
        kb_hash: str,
        node_id: str,
        direction: Literal["out", "in", "both"] = "both",
        depth: Annotated[int, Query(ge=1, le=3)] = 1,
        relation_alias: str | None = None,
        relation_type: str | None = None,
        declared_in: Literal["frontmatter", "body"] | None = None,
        validation_status: ValidationStatus | None = None,
        min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
        bundle: str | None = None,
        type_family: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    ) -> dict[str, Any]:
        """Neighborhood of a node. Edge filters apply to the edges walked, node filters to the nodes reached."""
        nav = get_nav(kb_hash)
        require_node(nav.snap, node_id)
        return nav.neighbors(
            node_id,
            direction=direction,
            depth=depth,
            edge_ok=nav.edge_filter(EdgeCriteria(relation_alias, relation_type, declared_in, validation_status, min_confidence)),
            node_ok=nav.node_filter(NodeCriteria(bundle, type_family, tag, status)),
            limit=limit,
        )

    @app.get("/kb/{kb_hash}/path")
    def shortest_path(
        kb_hash: str,
        from_id: Annotated[str, Query(alias="from")],
        to_id: Annotated[str, Query(alias="to")],
        directed: bool = False,
        relation_alias: str | None = None,
        relation_type: str | None = None,
        declared_in: Literal["frontmatter", "body"] | None = None,
        validation_status: ValidationStatus | None = None,
        min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
        avoid_type: Annotated[list[str] | None, Query(description="Do not pass through nodes of this type or type family")] = None,
        max_via_degree: Annotated[int | None, Query(ge=1, description="Do not pass through nodes with more neighbors than this")] = None,
    ) -> dict[str, Any]:
        """Shortest path by number of links. Undirected unless ``directed=true``.

        Hub pages (for example a schema overview with hundreds of links) create shortcuts that carry
        little meaning. ``avoid_type`` and ``max_via_degree`` keep paths off them.
        """
        nav = get_nav(kb_hash)
        require_node(nav.snap, from_id)
        require_node(nav.snap, to_id)
        crit = EdgeCriteria(relation_alias, relation_type, declared_in, validation_status, min_confidence)
        path = nav.shortest_path(
            from_id, to_id, directed=directed, edge_ok=nav.edge_filter(crit),
            via_ok=nav.via_filter(avoid_type, max_via_degree),
        )
        if path is None:
            raise HTTPException(status_code=404, detail="no path between the nodes with these filters")
        return {"kb_hash": kb_hash, "from": from_id, "to": to_id, "directed": directed, **path}

    @app.get("/kb/{kb_hash}/search")
    def search(
        kb_hash: str,
        q: Annotated[str, Query(min_length=1)],
        bundle: str | None = None,
        type_family: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Keyword search over title, tags, type, path, resource and description."""
        nav = get_nav(kb_hash)
        total, hits = search_indexes[kb_hash].search(q, nav.node_filter(NodeCriteria(bundle, type_family, tag, status)), limit)
        return {
            "kb_hash": kb_hash,
            "query": q,
            "total": total,
            "items": [{**h.to_dict(), "node": nav.node_summary(h.node_id)} for h in hits],
        }

    def semantic_status(kb_hash: str) -> dict[str, Any]:
        svc = semantic.get(kb_hash)
        return svc.status() if svc else {"state": "disabled", "error": "no embedding model configured"}

    @app.get("/kb/{kb_hash}/search/semantic/status")
    def semantic_search_status(kb_hash: str) -> dict[str, Any]:
        get_snapshot(kb_hash)
        return semantic_status(kb_hash)

    @app.get("/kb/{kb_hash}/search/semantic")
    def semantic_search(
        kb_hash: str,
        q: Annotated[str, Query(min_length=1)],
        top_k: Annotated[int, Query(ge=1, le=100)] = 10,
        expand_hops: Annotated[int, Query(ge=0, le=sem.MAX_HOPS)] = sem.DEFAULT_HOPS,
        alpha: Annotated[float, Query(gt=0, le=5)] = sem.DEFAULT_ALPHA,
        keyword_weight: Annotated[float, Query(ge=0, le=1)] = sem.DEFAULT_KEYWORD_WEIGHT,
        min_highlight: Annotated[float, Query(ge=0, le=1)] = sem.DEFAULT_MIN_HIGHLIGHT,
        max_via_degree: Annotated[int | None, Query(ge=1)] = sem.DEFAULT_HUB_DEGREE,
        max_expanded: Annotated[int, Query(ge=0, le=sem.MAX_EXPANDED)] = sem.DEFAULT_MAX_EXPANDED,
        bundle: str | None = None,
        type_family: str | None = None,
        tag: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Direct matches (cosine blended with keyword score) plus nodes highlighted by graph distance:
        highlight(v) = max over matches m of match_score(m) * exp(-alpha * dist(m, v))."""
        nav = get_nav(kb_hash)
        svc = semantic.get(kb_hash)
        if svc is None or svc.state != "ready":
            raise HTTPException(status_code=503, detail=semantic_status(kb_hash))
        hits = svc.search(
            q, top_k, expand_hops, alpha, keyword_weight, min_highlight, max_via_degree,
            nav.node_filter(NodeCriteria(bundle, type_family, tag, status)), max_expanded,
        )
        return {
            "kb_hash": kb_hash,
            "query": q,
            "params": {"top_k": top_k, "expand_hops": expand_hops, "alpha": alpha, "keyword_weight": keyword_weight,
                       "min_highlight": min_highlight, "max_via_degree": max_via_degree, "max_expanded": max_expanded},
            "model": svc.provider.model_id,
            "direct": sum(1 for h in hits if h.match_type == "direct"),
            "items": [{**h.to_dict(), "node": nav.node_summary(h.node_id)} for h in hits],
        }

    @app.get("/kb/{kb_hash}/tags")
    def tags(
        kb_hash: str,
        namespace: str | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    ) -> dict[str, Any]:
        return get_nav(kb_hash).tags(namespace, q, limit)

    @app.get("/kb/{kb_hash}/tag/{tag:path}/neighbors")
    def tag_neighbors(kb_hash: str, tag: str, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, Any]:
        """Members of a tag and the nodes just outside it that they link to."""
        nav = get_nav(kb_hash)
        if tag not in nav.tag_index:
            raise HTTPException(status_code=404, detail=f"unknown tag: {tag}")
        return nav.tag_neighbors(tag, limit)

    @app.get("/kb/{kb_hash}/topology")
    def topology(kb_hash: str, top: Annotated[int, Query(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Connected components and articulation points."""
        return get_nav(kb_hash).topology(top)

    def session_id_ok(session_id: str) -> str:
        if not SESSION_ID_RE.match(session_id):
            raise HTTPException(status_code=422, detail="session_id must match [A-Za-z0-9_-]{1,64}")
        return session_id

    def title_of(kb_hash: str, node_id: str) -> str | None:
        snap = by_hash.get(kb_hash)
        node = snap.nodes.get(node_id) if snap else None
        return node.title if node else None

    @app.post("/session/{session_id}/visit")
    def session_visit(session_id: str, request: Annotated[VisitRequest, Body()]) -> dict[str, Any]:
        session_id_ok(session_id)
        snap = get_snapshot(request.kb_hash)
        require_node(snap, request.node_id)
        v = sessions.visit(session_id, request.kb_hash, request.node_id, request.via)
        return {
            "session_id": session_id,
            "position": len(sessions.trail(session_id) or ()),
            "visit": {"kb_hash": v.kb_hash, "node_id": v.node_id, "title": title_of(v.kb_hash, v.node_id), "at": v.at, "via": v.via},
        }

    @app.get("/session/{session_id}/trail")
    def session_trail(session_id: str) -> dict[str, Any]:
        session_id_ok(session_id)
        trail = sessions.trail(session_id)
        if trail is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return {
            "session_id": session_id,
            "current": trail[-1].node_id,
            "items": [
                {"kb_hash": v.kb_hash, "node_id": v.node_id, "title": title_of(v.kb_hash, v.node_id), "at": v.at, "via": v.via}
                for v in trail
            ],
        }

    @app.get("/session/{session_id}/jump-back-options")
    def session_jump_back(session_id: str, limit: Annotated[int, Query(ge=1, le=100)] = 10) -> dict[str, Any]:
        """Earlier nodes, most recent first, with how many of their neighbors the session has not visited."""
        session_id_ok(session_id)
        options = sessions.jump_back_options(session_id, limit)
        if options is None:
            raise HTTPException(status_code=404, detail="unknown session")
        visited = {(v.kb_hash, v.node_id) for v in sessions.trail(session_id) or ()}
        for o in options:
            nav = navigators.get(o["kb_hash"])
            o["title"] = title_of(o["kb_hash"], o["node_id"])
            o["unvisited_neighbors"] = (
                len({other for other, _ in nav.steps(o["node_id"], "both") if (o["kb_hash"], other) not in visited})
                if nav and o["node_id"] in nav.adj
                else None
            )
        return {"session_id": session_id, "items": options}

    @app.get("/kb/{kb_hash}/summary")
    def summary(kb_hash: str) -> dict[str, Any]:
        return {**get_analysis(kb_hash).summary(), "semantic": semantic_status(kb_hash)}

    return app
