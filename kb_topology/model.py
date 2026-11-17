"""Canonical topology model built from an OKF bundle (see design doc, section 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class KbRef:
    """Pointer back into the KB. The index never stores document content."""

    path: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class Node:
    id: str  # "<env>:<path>", e.g. "prod:/area/group/item.md"
    env: str
    path: str  # KB-relative path with a leading "/"
    title: str
    type: str
    type_family: str
    type_qualifier: str | None
    bundle: str
    tags: tuple[str, ...]
    status: str | None
    source_refs: tuple[str, ...]
    kb_ref: KbRef
    description: str | None = None  # first 300 characters of frontmatter description
    metadata: dict[str, Any] = field(default_factory=dict)  # small declared scalars only


@dataclass(frozen=True, slots=True)
class Edge:
    source_id: str
    target_id: str
    declared_in: tuple[str, ...]  # "frontmatter" and/or "body"
    declared_bundle: str | None  # declared: references[].bundle
    declared_title: str | None  # declared: references[].title
    provenance: str | None  # declared: source node's generated.by
    declared_status: str | None  # declared: source node's status
    resolved: bool  # derived: target exists as a concept
    relation_type: str | None  # derived: "<source family> -> <target family>"
    relation_alias: str | None  # derived from alias rules, see aliases.py
    confidence: float | None = None  # derived by the validator, null until scored
    declared_ref_index: int | None = None  # position in frontmatter references[], if declared there


@dataclass(slots=True)
class Diagnostics:
    md_files: int = 0
    concepts: int = 0
    skipped_no_type: list[str] = field(default_factory=list)
    invalid_frontmatter: list[dict[str, str]] = field(default_factory=list)
    malformed_references: list[dict[str, Any]] = field(default_factory=list)
    links_to_non_concept: int = 0
    unresolved_edges: int = 0
    isolated_nodes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "md_files": self.md_files,
            "concepts": self.concepts,
            "skipped_no_type": self.skipped_no_type,
            "invalid_frontmatter": self.invalid_frontmatter,
            "malformed_references": self.malformed_references,
            "links_to_non_concept": self.links_to_non_concept,
            "unresolved_edges": self.unresolved_edges,
            "isolated_nodes": self.isolated_nodes,
        }


@dataclass(slots=True)
class Snapshot:
    env: str
    kb_hash: str
    version: str | None
    nodes: dict[str, Node]
    edges: list[Edge]
    diagnostics: Diagnostics
    root: Path | None = None  # bundle directory, used to read concept files on demand
    forward: dict[str, list[int]] = field(default_factory=dict)  # node id -> edge indexes
    reverse: dict[str, list[int]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "kb_hash": self.kb_hash,
            "env": self.env,
            "version": self.version,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "unresolved_edges": self.diagnostics.unresolved_edges,
            "isolated_nodes": self.diagnostics.isolated_nodes,
        }
