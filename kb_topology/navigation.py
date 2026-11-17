"""Graph navigation over a snapshot (design doc, sections 5.3 and 5.6).

All traversals use resolved edges only. Dangling edges are findings, not graph structure.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .analysis import Analysis
from .model import Edge, Node

EdgeFilter = Callable[[int, Edge], bool]
NodeFilter = Callable[[Node], bool]


def edge_to_dict(edge: Edge) -> dict[str, Any]:
    data = asdict(edge)
    data["declared_in"] = list(edge.declared_in)
    return data


@dataclass(slots=True)
class EdgeCriteria:
    relation_alias: str | None = None
    relation_type: str | None = None
    declared_in: str | None = None
    validation_status: str | None = None
    min_confidence: float | None = None


@dataclass(slots=True)
class NodeCriteria:
    bundle: str | None = None
    type_family: str | None = None
    tag: str | None = None
    status: str | None = None


class Navigator:
    def __init__(self, analysis: Analysis) -> None:
        self.analysis = analysis
        self.snap = analysis.snapshot
        # node id -> list of (neighbor id, edge index, outgoing?)
        self.adj: dict[str, list[tuple[str, int, bool]]] = {nid: [] for nid in self.snap.nodes}
        for i, e in enumerate(self.snap.edges):
            if e.resolved:
                self.adj[e.source_id].append((e.target_id, i, True))
                self.adj[e.target_id].append((e.source_id, i, False))
        self.tag_index: dict[str, list[str]] = {}
        for nid, n in self.snap.nodes.items():
            for t in n.tags:
                self.tag_index.setdefault(t, []).append(nid)
        self.degree: dict[str, int] = {nid: len(self._simple_neighbors(nid)) for nid in self.snap.nodes}
        self.component_of: dict[str, int] = {}
        self.component_sizes: list[int] = []
        self._label_components()
        self.articulation_points: set[str] = self._articulation_points()

    # ---- topology -------------------------------------------------------

    def _simple_neighbors(self, nid: str) -> set[str]:
        return {other for other, _, _ in self.adj[nid] if other != nid}

    def _label_components(self) -> None:
        for start in sorted(self.snap.nodes):
            if start in self.component_of:
                continue
            cid = len(self.component_sizes)
            self.component_of[start] = cid
            queue, size = deque([start]), 0
            while queue:
                cur = queue.popleft()
                size += 1
                for other in self._simple_neighbors(cur):
                    if other not in self.component_of:
                        self.component_of[other] = cid
                        queue.append(other)
            self.component_sizes.append(size)
        # Re-number so component 0 is the largest (ties keep discovery order).
        order = sorted(range(len(self.component_sizes)), key=lambda c: (-self.component_sizes[c], c))
        remap = {old: new for new, old in enumerate(order)}
        self.component_of = {n: remap[c] for n, c in self.component_of.items()}
        self.component_sizes = [self.component_sizes[c] for c in order]

    def _articulation_points(self) -> set[str]:
        """Iterative Tarjan over the undirected simple graph."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        result: set[str] = set()
        counter = 0
        neighbors = {nid: sorted(self._simple_neighbors(nid)) for nid in self.snap.nodes}
        for root in sorted(self.snap.nodes):
            if root in index:
                continue
            index[root] = low[root] = counter
            counter += 1
            root_children = 0
            stack: list[tuple[str, str | None, int]] = [(root, None, 0)]
            while stack:
                node, parent, i = stack.pop()
                nbrs = neighbors[node]
                if i < len(nbrs):
                    stack.append((node, parent, i + 1))
                    other = nbrs[i]
                    if other == parent:
                        continue
                    if other in index:
                        low[node] = min(low[node], index[other])
                    else:
                        index[other] = low[other] = counter
                        counter += 1
                        if node == root:
                            root_children += 1
                        stack.append((other, node, 0))
                elif parent is not None:
                    low[parent] = min(low[parent], low[node])
                    if parent != root and low[node] >= index[parent]:
                        result.add(parent)
            if root_children > 1:
                result.add(root)
        return result

    def topology(self, top: int = 20) -> dict[str, Any]:
        sizes = self.component_sizes
        by_degree = sorted(self.articulation_points, key=lambda n: (-self.degree[n], n))
        return {
            "kb_hash": self.snap.kb_hash,
            "nodes": len(self.snap.nodes),
            "components": {
                "count": len(sizes),
                "largest": sizes[0] if sizes else 0,
                "singletons": sum(1 for s in sizes if s == 1),
                "top_sizes": sizes[:top],
            },
            "articulation_points": {
                "count": len(self.articulation_points),
                "top_by_degree": [self.node_summary(n) for n in by_degree[:top]],
            },
        }

    # ---- summaries and filters -------------------------------------------

    def node_summary(self, nid: str) -> dict[str, Any]:
        n = self.snap.nodes[nid]
        return {
            "id": n.id,
            "title": n.title,
            "type": n.type,
            "type_family": n.type_family,
            "bundle": n.bundle,
            "status": n.status,
            "tags": list(n.tags),
            "degree": self.degree[nid],
            "component_id": self.component_of[nid],
            "is_articulation_point": nid in self.articulation_points,
        }

    def edge_dict(self, idx: int) -> dict[str, Any]:
        d = edge_to_dict(self.snap.edges[idx])
        d["validation_status"] = self.analysis.validations[idx].validation_status
        return d

    def edge_filter(self, c: EdgeCriteria) -> EdgeFilter:
        def ok(i: int, e: Edge) -> bool:
            return (
                (c.relation_alias is None or e.relation_alias == c.relation_alias)
                and (c.relation_type is None or e.relation_type == c.relation_type)
                and (c.declared_in is None or c.declared_in in e.declared_in)
                and (c.min_confidence is None or (e.confidence or 0.0) >= c.min_confidence)
                and (c.validation_status is None or self.analysis.validations[i].validation_status == c.validation_status)
            )

        return ok

    @staticmethod
    def node_filter(c: NodeCriteria) -> NodeFilter:
        def ok(n: Node) -> bool:
            return (
                (c.bundle is None or n.bundle == c.bundle)
                and (c.type_family is None or n.type_family == c.type_family)
                and (c.tag is None or c.tag in n.tags)
                and (c.status is None or n.status == c.status)
            )

        return ok

    # ---- traversal ---------------------------------------------------------

    def steps(self, nid: str, direction: str):
        for other, idx, outgoing in self.adj[nid]:
            if direction == "both" or (direction == "out" and outgoing) or (direction == "in" and not outgoing):
                yield other, idx

    def neighbors(
        self,
        nid: str,
        direction: str = "both",
        depth: int = 1,
        edge_ok: EdgeFilter | None = None,
        node_ok: NodeFilter | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Breadth-first neighborhood. Nodes are ordered by distance, then title."""
        seen: dict[str, int] = {nid: 0}
        via: dict[str, int] = {}
        frontier = [nid]
        for d in range(1, depth + 1):
            nxt: list[str] = []
            for cur in frontier:
                for other, idx in self.steps(cur, direction):
                    if other in seen:
                        continue
                    if (edge_ok and not edge_ok(idx, self.snap.edges[idx])) or (
                        node_ok and not node_ok(self.snap.nodes[other])
                    ):
                        continue
                    seen[other] = d
                    via[other] = idx
                    nxt.append(other)
            frontier = nxt
        found = sorted((n for n in seen if n != nid), key=lambda n: (seen[n], self.snap.nodes[n].title, n))
        shown = found[:limit]
        return {
            "node": self.node_summary(nid),
            "depth": depth,
            "direction": direction,
            "total": len(found),
            "truncated": len(found) > len(shown),
            "neighbors": [
                {"distance": seen[n], "node": self.node_summary(n), "via_edge": self.edge_dict(via[n])} for n in shown
            ],
        }

    def shortest_path(
        self,
        source: str,
        target: str,
        directed: bool = False,
        edge_ok: EdgeFilter | None = None,
        via_ok: NodeFilter | None = None,
    ) -> dict[str, Any] | None:
        """Shortest path by link count. ``via_ok`` limits which nodes the path may pass through (not the endpoints)."""
        if source == target:
            return {"length": 0, "nodes": [self.node_summary(source)], "edges": []}
        direction = "out" if directed else "both"
        parent: dict[str, tuple[str, int]] = {}
        seen = {source}
        queue = deque([source])
        while queue:
            cur = queue.popleft()
            for other, idx in self.steps(cur, direction):
                if other in seen or (edge_ok and not edge_ok(idx, self.snap.edges[idx])):
                    continue
                if other != target and via_ok and not via_ok(self.snap.nodes[other]):
                    continue
                seen.add(other)
                parent[other] = (cur, idx)
                if other == target:
                    return self._unwind(source, target, parent)
                queue.append(other)
        return None

    def _unwind(self, source: str, target: str, parent: dict[str, tuple[str, int]]) -> dict[str, Any]:
        nodes, edges = [target], []
        cur = target
        while cur != source:
            prev, idx = parent[cur]
            edges.append(idx)
            nodes.append(prev)
            cur = prev
        nodes.reverse()
        edges.reverse()
        return {
            "length": len(edges),
            "nodes": [self.node_summary(n) for n in nodes],
            "edges": [self.edge_dict(i) for i in edges],
        }

    def via_filter(self, avoid_types: list[str] | None, max_degree: int | None) -> NodeFilter | None:
        """Predicate for nodes a path may pass through. None when no restriction is requested."""
        if not avoid_types and max_degree is None:
            return None
        avoid = set(avoid_types or ())

        def ok(n: Node) -> bool:
            return n.type not in avoid and n.type_family not in avoid and (
                max_degree is None or self.degree[n.id] <= max_degree
            )

        return ok

    # ---- tags ------------------------------------------------------------------

    def tags(self, namespace: str | None = None, q: str | None = None, limit: int = 200) -> dict[str, Any]:
        counts = Counter({t: len(ids) for t, ids in self.tag_index.items()})
        namespaces = Counter()
        for t, n in counts.items():
            namespaces[t.split(":", 1)[0] if ":" in t else ""] += n
        items = [
            (t, n)
            for t, n in counts.items()
            if (namespace is None or t.startswith(f"{namespace}:"))
            and (q is None or q.lower() in t.lower())
        ]
        items.sort(key=lambda x: (-x[1], x[0]))
        return {
            "total": len(items),
            "namespaces": dict(namespaces.most_common()),
            "items": [{"tag": t, "count": n} for t, n in items[:limit]],
        }

    def tag_neighbors(self, tag: str, limit: int = 100) -> dict[str, Any]:
        """Members of a tag plus the nodes just outside it that members link to, by link count."""
        members = self.tag_index.get(tag, [])
        member_set = set(members)
        outside: Counter[str] = Counter()
        for m in members:
            for other, _ in self.steps(m, "both"):
                if other not in member_set:
                    outside[other] += 1
        ranked = sorted(outside.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            "tag": tag,
            "members": len(members),
            "sample_members": [self.node_summary(m) for m in sorted(members)[:limit]],
            "boundary_total": len(outside),
            "boundary": [{"links_from_members": c, "node": self.node_summary(n)} for n, c in ranked[:limit]],
        }
