from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations
import re
from typing import Any


TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
REFERENCE_MENTION_RE = re.compile(r"\b[a-zA-Z]{3,}(?:[-._][a-zA-Z0-9]+){1,}\b")


def _normalize_tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(value)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _sanitize_identifier(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


def _escape_mermaid_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class Concept:
    concept_id: str
    title: str
    bundle: str
    tags: set[str]
    text: str

    @property
    def lexical_signature(self) -> set[str]:
        return _normalize_tokens(self.title) | _normalize_tokens(self.text) | {tag.lower() for tag in self.tags}


@dataclass(frozen=True)
class CrossLinkSuggestion:
    source_id: str
    target_id: str
    score: float
    reason: str


@dataclass(frozen=True)
class ReferenceRepairSuggestion:
    source_id: str
    target_id: str
    mention: str
    confidence: float
    ambiguous: bool
    reason: str


@dataclass(frozen=True)
class RelationRecord:
    source_id: str
    target_id: str
    relation_type: str
    provenance: str = "manual"
    confidence: float = 1.0
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TopologyDiagnostics:
    connected_components_count: int
    connected_components: list[set[str]]
    orphan_concepts: set[str]
    articulation_points: set[str]
    sparse_concepts: set[str]


class TopologyKnowledgeGraph:
    """
    Topology-inspired KB graph:
    - Local neighborhoods model semantic "open sets"
    - Components/articulation points expose weak connectivity
    - Link suggestions maximize neighborhood overlap with continuity constraints
    """

    def __init__(self) -> None:
        self._concepts: dict[str, Concept] = {}
        self._adjacency: dict[str, set[str]] = defaultdict(set)
        self._relations: dict[tuple[str, str], RelationRecord] = {}

    def add_concept(
        self,
        concept_id: str,
        title: str,
        bundle: str,
        tags: set[str] | list[str] | tuple[str, ...],
        text: str,
    ) -> None:
        if concept_id in self._concepts:
            raise ValueError(f"Concept already exists: {concept_id}")
        normalized_tags = {tag.strip().lower() for tag in tags if tag.strip()}
        self._concepts[concept_id] = Concept(
            concept_id=concept_id,
            title=title.strip(),
            bundle=bundle.strip().lower(),
            tags=normalized_tags,
            text=text.strip(),
        )
        self._adjacency.setdefault(concept_id, set())

    def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str = "related",
        provenance: str = "manual",
        confidence: float = 1.0,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if source_id == target_id:
            raise ValueError("Self-relations are not allowed")
        if source_id not in self._concepts or target_id not in self._concepts:
            raise ValueError("Both concepts must exist before adding a relation")

        left, right = sorted((source_id, target_id))
        self._relations[(left, right)] = RelationRecord(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type.strip().lower(),
            provenance=provenance.strip().lower() if provenance else "manual",
            confidence=float(confidence),
            status=status.strip().lower() if status else "active",
            metadata=dict(metadata or {}),
        )
        self._adjacency[source_id].add(target_id)
        self._adjacency[target_id].add(source_id)

    def annotate_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str | None = None,
        provenance: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        left, right = sorted((source_id, target_id))
        if (left, right) not in self._relations:
            raise ValueError(f"No relation exists between {source_id} and {target_id}")

        existing = self._relations[(left, right)]
        self._relations[(left, right)] = RelationRecord(
            source_id=existing.source_id,
            target_id=existing.target_id,
            relation_type=(relation_type or existing.relation_type).strip().lower(),
            provenance=(provenance or existing.provenance).strip().lower(),
            confidence=float(confidence if confidence is not None else existing.confidence),
            status=(status or existing.status).strip().lower(),
            metadata={**existing.metadata, **(metadata or {})},
        )

    def neighborhood(self, concept_id: str, depth: int = 1) -> set[str]:
        # Topology mapping: this is a finite-graph analog of an open neighborhood
        # around concept_id. depth=1 gives the immediate local neighborhood, larger
        # depths approximate expanded local context used for reasoning and navigation.
        if concept_id not in self._concepts:
            raise ValueError(f"Unknown concept_id: {concept_id}")
        if depth < 1:
            return {concept_id}

        visited = {concept_id}
        frontier = {concept_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                next_frontier |= self._adjacency[node]
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier
            if not frontier:
                break
        return visited

    def suggest_cross_links(self, top_k: int = 20, min_score: float = 0.2) -> list[CrossLinkSuggestion]:
        # Topology mapping: we prioritize links that preserve local continuity
        # (high overlap in nearby semantic features) and rank by overlap strength.
        suggestions: list[CrossLinkSuggestion] = []
        for source_id, target_id in combinations(self._concepts.keys(), 2):
            if self._has_relation(source_id, target_id):
                continue

            score, reason = self._score_candidate(source_id, target_id)
            if score >= min_score:
                suggestions.append(
                    CrossLinkSuggestion(
                        source_id=source_id,
                        target_id=target_id,
                        score=score,
                        reason=reason,
                    )
                )

        suggestions.sort(key=lambda item: item.score, reverse=True)
        return suggestions[:top_k]

    def suggest_reference_repairs(
        self,
        top_k: int = 20,
        min_confidence: float = 0.5,
    ) -> list[ReferenceRepairSuggestion]:
        # Topology mapping: this "repair" operator tries to close small breaks in the
        # graph caused by partial references in text (misspellings/incomplete IDs).
        # It picks candidate targets that best preserve local semantic continuity.
        best_by_pair: dict[tuple[str, str], ReferenceRepairSuggestion] = {}
        concept_items = sorted(self._concepts.items())

        for source_id, source in concept_items:
            mentions = self._extract_reference_mentions(f"{source.title} {source.text}")
            for mention in mentions:
                if mention == source_id:
                    continue

                if mention in self._concepts and not self._has_relation(source_id, mention):
                    # Exact unresolved mention: we treat this as a missing edge that
                    # should already exist in the graph.
                    candidate = ReferenceRepairSuggestion(
                        source_id=source_id,
                        target_id=mention,
                        mention=mention,
                        confidence=1.0,
                        ambiguous=False,
                        reason="exact unresolved reference in document text",
                    )
                    best_by_pair[(source_id, mention)] = candidate
                    continue

                ranked: list[tuple[float, str]] = []
                for target_id, target in concept_items:
                    if target_id == source_id or self._has_relation(source_id, target_id):
                        continue
                    ranked.append((self._score_reference_candidate(mention, target), target_id))

                if not ranked:
                    continue
                ranked.sort(reverse=True)
                best_score, best_target = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else 0.0
                confidence = best_score

                if confidence < min_confidence:
                    continue

                # Small score gap means several nodes are topologically "close";
                # we keep the candidate but mark it as ambiguous for human review.
                is_ambiguous = (best_score - second_score) < 0.05
                ambiguity = "ambiguous " if is_ambiguous else ""
                suggestion = ReferenceRepairSuggestion(
                    source_id=source_id,
                    target_id=best_target,
                    mention=mention,
                    confidence=confidence,
                    ambiguous=is_ambiguous,
                    reason=f"topology-aware repair for {ambiguity}partial reference".strip(),
                )
                pair = (source_id, best_target)
                current = best_by_pair.get(pair)
                if current is None or suggestion.confidence > current.confidence:
                    best_by_pair[pair] = suggestion

        suggestions = sorted(best_by_pair.values(), key=lambda item: item.confidence, reverse=True)
        return suggestions[:top_k]

    def topology_diagnostics(self) -> TopologyDiagnostics:
        # Topology mapping:
        # - connected_components -> global connectedness of the KB
        # - articulation_points -> cut vertices whose removal disconnects regions
        # - orphan/sparse nodes -> weakly integrated concepts
        components = self._connected_components()
        orphan_nodes = {node for node, neighbors in self._adjacency.items() if not neighbors}
        sparse_nodes = {node for node, neighbors in self._adjacency.items() if len(neighbors) <= 1}
        articulation = self._articulation_points()
        return TopologyDiagnostics(
            connected_components_count=len(components),
            connected_components=components,
            orphan_concepts=orphan_nodes,
            articulation_points=articulation,
            sparse_concepts=sparse_nodes,
        )

    @property
    def concept_count(self) -> int:
        return len(self._concepts)

    @property
    def relation_count(self) -> int:
        return len(self._relations)

    def concepts(self) -> list[Concept]:
        return sorted(self._concepts.values(), key=lambda concept: concept.concept_id)

    def relation_records(self) -> list[RelationRecord]:
        return [
            relation
            for (_, _), relation in sorted(self._relations.items(), key=lambda item: (item[0][0], item[0][1]))
        ]

    def relations(self) -> list[tuple[str, str, str]]:
        return [
            (relation.source_id, relation.target_id, relation.relation_type)
            for relation in self.relation_records()
        ]

    def clone(self) -> TopologyKnowledgeGraph:
        cloned = TopologyKnowledgeGraph()
        for concept in self.concepts():
            cloned.add_concept(
                concept_id=concept.concept_id,
                title=concept.title,
                bundle=concept.bundle,
                tags=concept.tags,
                text=concept.text,
            )
        for relation in self.relation_records():
            cloned.add_relation(
                relation.source_id,
                relation.target_id,
                relation_type=relation.relation_type,
                provenance=relation.provenance,
                confidence=relation.confidence,
                status=relation.status,
                metadata=relation.metadata,
            )
        return cloned

    def to_mermaid(self) -> str:
        node_ids = sorted(self._concepts)
        aliases = {concept_id: f"n{index:03d}" for index, concept_id in enumerate(node_ids)}

        lines = ["flowchart LR"]
        bundle_class_map: dict[str, str] = {}
        class_members: dict[str, list[str]] = defaultdict(list)

        for concept_id in node_ids:
            concept = self._concepts[concept_id]
            alias = aliases[concept_id]
            label = _escape_mermaid_label(concept_id)
            lines.append(f'    {alias}["{label}"]')

            class_name = _sanitize_identifier(concept.bundle)
            bundle_class_map[concept.bundle] = class_name
            class_members[class_name].append(alias)

        for left, right in sorted(self._relations):
            relation = _escape_mermaid_label(self._relations[(left, right)].relation_type)
            lines.append(f'    {aliases[left]} -- "{relation}" --> {aliases[right]}')

        for class_name, members in sorted(class_members.items()):
            lines.append(f"    class {','.join(members)} {class_name};")

        palette = (
            "#e3f2fd",
            "#e8f5e9",
            "#fff3e0",
            "#f3e5f5",
            "#fbe9e7",
            "#f1f8e9",
        )
        for index, bundle in enumerate(sorted(bundle_class_map)):
            class_name = bundle_class_map[bundle]
            fill = palette[index % len(palette)]
            lines.append(f"    classDef {class_name} fill:{fill},stroke:#546e7a,stroke-width:1px;")

        return "\n".join(lines)

    def _extract_reference_mentions(self, text: str) -> set[str]:
        lowered = text.lower()
        mentions = {_sanitize_identifier(match.group(0)).replace("_", "-") for match in REFERENCE_MENTION_RE.finditer(lowered)}
        return {mention for mention in mentions if len(mention) >= 5}

    def _score_reference_candidate(self, mention: str, target: Concept) -> float:
        mention_key = mention.lower()
        mention_tokens = _normalize_tokens(mention_key.replace("-", " "))
        target_id = target.concept_id.lower()
        target_title = target.title.lower()

        token_overlap = _jaccard(mention_tokens, target.lexical_signature)
        id_ratio = SequenceMatcher(None, mention_key, target_id).ratio()
        title_ratio = SequenceMatcher(None, mention_key.replace("-", " "), target_title).ratio()

        contains_bonus = 1.0 if mention_key in target_id or target_id in mention_key else 0.0
        # Weighted similarity behaves like a practical continuity proxy: the higher
        # this value, the less "jump" is needed to map mention -> target concept.
        score = (0.45 * id_ratio) + (0.30 * token_overlap) + (0.15 * title_ratio) + (0.10 * contains_bonus)
        return min(1.0, score)

    def _has_relation(self, source_id: str, target_id: str) -> bool:
        left, right = sorted((source_id, target_id))
        return (left, right) in self._relations

    def _score_candidate(self, source_id: str, target_id: str) -> tuple[float, str]:
        source = self._concepts[source_id]
        target = self._concepts[target_id]

        tag_overlap = _jaccard(source.tags, target.tags)
        lexical_overlap = _jaccard(source.lexical_signature, target.lexical_signature)
        same_bundle = source.bundle == target.bundle

        continuity_signal = max(tag_overlap, lexical_overlap)
        # Continuity threshold: reject candidates that create large semantic jumps.
        if continuity_signal < 0.08:
            return 0.0, "fails continuity threshold"

        # Scoring combines local similarity ("nearby points" in semantic space)
        # plus same-bundle proximity as a coarse neighborhood prior.
        score = (0.45 * tag_overlap) + (0.35 * lexical_overlap) + (0.20 * (1.0 if same_bundle else 0.0))

        if same_bundle:
            reason = "high local neighborhood overlap"
        elif continuity_signal >= 0.2:
            reason = "cross-bundle bridge candidate"
        else:
            reason = "weak lexical bridge"
        return score, reason

    def _connected_components(self) -> list[set[str]]:
        # Classical connected-components decomposition:
        # each component is a connected subspace of the graph.
        unvisited = set(self._concepts)
        components: list[set[str]] = []
        while unvisited:
            root = unvisited.pop()
            component = {root}
            frontier = [root]
            while frontier:
                node = frontier.pop()
                for neighbor in self._adjacency[node]:
                    if neighbor in unvisited:
                        unvisited.remove(neighbor)
                        component.add(neighbor)
                        frontier.append(neighbor)
            components.append(component)
        components.sort(key=len, reverse=True)
        return components

    def _articulation_points(self) -> set[str]:
        # Tarjan DFS for articulation points (cut vertices):
        # nodes whose removal increases number of connected components.
        discovery: dict[str, int] = {}
        low: dict[str, int] = {}
        parent: dict[str, str | None] = {}
        points: set[str] = set()
        timer = 0

        def dfs(node: str) -> None:
            nonlocal timer
            discovery[node] = timer
            low[node] = timer
            timer += 1
            children = 0

            for neighbor in self._adjacency[node]:
                if neighbor not in discovery:
                    parent[neighbor] = node
                    children += 1
                    dfs(neighbor)
                    low[node] = min(low[node], low[neighbor])

                    if parent.get(node) is None and children > 1:
                        points.add(node)
                    if parent.get(node) is not None and low[neighbor] >= discovery[node]:
                        points.add(node)
                elif neighbor != parent.get(node):
                    low[node] = min(low[node], discovery[neighbor])

        for node in self._concepts:
            if node not in discovery:
                parent[node] = None
                dfs(node)
        return points
