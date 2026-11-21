"""OKF bundle adapter: reads a bundle directory (read-only) into a Snapshot.

Design doc, section 5.1. The adapter never writes to the bundle and never reads its
``.cache`` directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .aliases import AliasRules, relation_type
from .model import Diagnostics, Edge, KbRef, Node, Snapshot

try:  # libyaml is several times faster on 15K files
    _YamlLoader: Any = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _YamlLoader = yaml.SafeLoader

# Files that are not concepts: bundle indexes, logs and readmes.
SKIP_DIRS = frozenset({".cache", ".git"})
SKIP_FILES = frozenset({"index.md", "log.md", "README.md"})

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_LINK_RE = re.compile(r"""\]\(([^)\s]+)|href=["']([^"'>\s]+)""")
_EXTERNAL_RE = re.compile(r"^(?:https?:|mailto:|ftp:|tel:|data:|javascript:|//|#)", re.IGNORECASE)

FRONTMATTER = "frontmatter"
BODY = "body"


def resolve_href(href: str, source_path: str) -> str:
    """Resolve ``href`` against ``source_path`` (both with a leading "/"). Collapses ``.`` and ``..``."""
    if not href.startswith("/"):
        base = source_path.rsplit("/", 1)[0]
        href = f"{base}/{href}"
    parts: list[str] = []
    for part in href.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/" + "/".join(parts)


def extract_body_links(body: str, source_path: str) -> list[str]:
    """Return resolved ``.md`` link targets found in a concept body, in order, without duplicates."""
    seen: dict[str, None] = {}
    for m in _LINK_RE.finditer(body):
        href = (m.group(1) or m.group(2) or "").strip().rstrip("\"'>)].")
        if not href or _EXTERNAL_RE.match(href):
            continue
        href = href.split("#", 1)[0].split("?", 1)[0]
        if not href.lower().endswith(".md"):
            continue
        seen[resolve_href(href, source_path)] = None
    return list(seen)


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return (frontmatter, body, error). frontmatter is None when absent or invalid."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text, None
    body = text[m.end():]
    try:
        data = yaml.load(m.group(1), Loader=_YamlLoader)
    except yaml.YAMLError as exc:
        return None, body, str(exc).splitlines()[0]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, body, "frontmatter is not a mapping"
    return data, body, None


_MODELED_KEYS = frozenset({"type", "title", "tags", "status", "sources", "references", "description", "generated", "verified"})


def _scalar_metadata(fm: dict[str, Any]) -> dict[str, Any]:
    """Small scalar frontmatter values not already modeled (e.g. row_count, schedule)."""
    return {
        k: v
        for k, v in fm.items()
        if k not in _MODELED_KEYS
        and isinstance(v, (str, int, float, bool))
        and not (isinstance(v, str) and len(v) > 200)
    }


def _short_description(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:300]


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if v is not None)


@dataclass(slots=True)
class _Parsed:
    path: str
    content_hash: str
    frontmatter: dict[str, Any] | None
    error: str | None
    body_links: list[str] = field(default_factory=list)


def _parse_file(root: Path, rel: str) -> _Parsed:
    raw = (root / rel.lstrip("/")).read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    fm, body, error = split_frontmatter(raw.decode("utf-8", errors="replace"))
    links = extract_body_links(body, rel) if fm is not None and fm.get("type") else []
    return _Parsed(rel, content_hash, fm, error, links)


def _iter_md_paths(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".md") and name not in SKIP_FILES:
                rel = "/" + os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                out.append(rel)
    out.sort()
    return out


def _read_marker(root: Path, name: str) -> str | None:
    p = root / name
    return p.read_text(encoding="utf-8").strip() if p.is_file() else None


def load_snapshot(
    root: str | Path,
    env: str | None = None,
    workers: int = 8,
    alias_rules: AliasRules | None = None,
    documents: Iterable[tuple[str, dict[str, Any], str]] | None = None,
) -> Snapshot:
    """Build a Snapshot from the OKF bundle at ``root`` for environment ``env``.

    ``env`` defaults to the bundle directory name (``.okf/prod`` gives ``prod``). ``alias_rules`` sets
    ``relation_alias`` on edges (see aliases.py); without rules it stays None.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"OKF bundle directory not found: {root}")
    env = env or root.name
    diag = Diagnostics()

    if documents is None:
        paths = _iter_md_paths(root)
        diag.md_files = len(paths)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parsed = list(pool.map(lambda p: _parse_file(root, p), paths))
    else:
        parsed = []
        for path, frontmatter, body in documents:
            rel = path if path.startswith("/") else "/" + path
            canonical = yaml.safe_dump(frontmatter, sort_keys=True).encode("utf-8") + body.encode("utf-8")
            content_hash = hashlib.sha256(canonical).hexdigest()
            links = extract_body_links(body, rel) if frontmatter.get("type") else []
            parsed.append(_Parsed(rel, content_hash, frontmatter, None, links))
        parsed.sort(key=lambda p: p.path)
        diag.md_files = len(parsed)

    version = _read_marker(root, "VERSION")
    digest = hashlib.sha256("\n".join([env, version or ""]).encode())
    for p in parsed:
        digest.update(f"\n{p.path}:{p.content_hash}".encode())
    kb_hash = digest.hexdigest()[:16]

    nodes: dict[str, Node] = {}
    concepts: list[_Parsed] = []
    existing = {p.path for p in parsed}  # every md file, concept or not
    for p in parsed:
        if p.error:
            diag.invalid_frontmatter.append({"path": p.path, "error": p.error})
        fm = p.frontmatter
        type_raw = fm.get("type") if fm else None
        if not isinstance(type_raw, str) or not type_raw.strip():
            diag.skipped_no_type.append(p.path)
            continue
        assert fm is not None
        family, _, qualifier = type_raw.strip().partition(":")
        sources = fm.get("sources")
        source_refs = tuple(
            str(s.get("resource") or s.get("id"))
            for s in (sources if isinstance(sources, list) else [])
            if isinstance(s, dict) and (s.get("resource") or s.get("id"))
        )
        stem = p.path.rsplit("/", 1)[-1].removesuffix(".md")
        node = Node(
            id=f"{env}:{p.path}",
            env=env,
            path=p.path,
            title=str(fm.get("title") or stem),
            type=type_raw.strip(),
            type_family=family,
            type_qualifier=qualifier or None,
            bundle=p.path.split("/")[1] if p.path.count("/") > 1 else "",
            tags=_as_str_tuple(fm.get("tags")),
            status=str(fm["status"]) if fm.get("status") is not None else None,
            source_refs=source_refs,
            kb_ref=KbRef(path=p.path, content_hash=p.content_hash),
            description=_short_description(fm.get("description")),
            metadata=_scalar_metadata(fm),
        )
        nodes[node.id] = node
        concepts.append(p)
    diag.concepts = len(nodes)

    edges = _build_edges(env, concepts, nodes, existing, diag, alias_rules or AliasRules())
    snapshot = Snapshot(
        env=env,
        kb_hash=kb_hash,
        version=version,
        nodes=nodes,
        edges=edges,
        diagnostics=diag,
        root=root,
    )
    _index_edges(snapshot)
    return snapshot


@dataclass(slots=True)
class _EdgeDraft:
    declared_in: dict[str, None] = field(default_factory=dict)
    declared_bundle: str | None = None
    declared_title: str | None = None
    ref_index: int | None = None


def _build_edges(
    env: str,
    concepts: list[_Parsed],
    nodes: dict[str, Node],
    existing: set[str],
    diag: Diagnostics,
    aliases: AliasRules,
) -> list[Edge]:
    edges: list[Edge] = []
    for p in concepts:
        fm = p.frontmatter or {}
        src_id = f"{env}:{p.path}"
        src = nodes[src_id]
        generated = fm.get("generated")
        provenance = str(generated["by"]) if isinstance(generated, dict) and generated.get("by") else None
        drafts: dict[str, _EdgeDraft] = {}

        refs = fm.get("references")
        for i, ref in enumerate(refs if isinstance(refs, list) else []):
            ref_id = ref.get("id") if isinstance(ref, dict) else None
            if not isinstance(ref_id, str) or not ref_id.strip():
                diag.malformed_references.append({"path": p.path, "index": i})
                continue
            target = resolve_href(ref_id.strip(), p.path)
            if target == p.path:
                continue
            draft = drafts.setdefault(target, _EdgeDraft())
            draft.declared_in[FRONTMATTER] = None
            if draft.ref_index is None:
                draft.ref_index = i
            if draft.declared_title is None:
                draft.declared_bundle = str(ref["bundle"]) if ref.get("bundle") is not None else None
                draft.declared_title = str(ref["title"]) if ref.get("title") is not None else None
        for target in p.body_links:
            if target != p.path:
                drafts.setdefault(target, _EdgeDraft()).declared_in[BODY] = None

        for target, draft in drafts.items():
            tgt_id = f"{env}:{target}"
            tgt = nodes.get(tgt_id)
            if tgt is None and target in existing:
                diag.links_to_non_concept += 1  # file exists but is not a concept (e.g. no type)
                continue
            if tgt is None and target.rsplit("/", 1)[-1] in SKIP_FILES:
                diag.links_to_non_concept += 1  # index.md, log.md, README.md
                continue
            edges.append(
                Edge(
                    source_id=src_id,
                    target_id=tgt_id,
                    declared_in=tuple(draft.declared_in),
                    declared_bundle=draft.declared_bundle,
                    declared_title=draft.declared_title,
                    provenance=provenance,
                    declared_status=src.status,
                    resolved=tgt is not None,
                    relation_type=relation_type(src.type_family, tgt.type_family) if tgt else None,
                    relation_alias=aliases.alias(src.type_family, tgt.type_family) if tgt else None,
                    declared_ref_index=draft.ref_index,
                )
            )
            if tgt is None:
                diag.unresolved_edges += 1
    return edges


def _index_edges(snapshot: Snapshot) -> None:
    forward = snapshot.forward
    reverse = snapshot.reverse
    for i, e in enumerate(snapshot.edges):
        forward.setdefault(e.source_id, []).append(i)
        if e.resolved:
            reverse.setdefault(e.target_id, []).append(i)
    snapshot.diagnostics.isolated_nodes = sum(
        1 for nid in snapshot.nodes if nid not in forward and nid not in reverse
    )
