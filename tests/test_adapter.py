from pathlib import Path

import pytest

from conftest import RULES
from kb_topology.adapter import extract_body_links, load_snapshot, resolve_href

R1 = "prod:/docs/guides/r1.md"
C1 = "prod:/docs/procedures/c1.md"
T1 = "prod:/data/tables/T1.md"
T2 = "prod:/data/tables/T2.md"
K1 = "prod:/feeds/streams/k1.md"


def edge(snap, src, tgt):
    matches = [e for e in snap.edges if e.source_id == src and e.target_id == tgt]
    assert len(matches) == 1, (src, tgt, matches)
    return matches[0]


def test_resolve_href():
    assert resolve_href("/a/b.md", "/x/y.md") == "/a/b.md"
    assert resolve_href("b.md", "/x/y.md") == "/x/b.md"
    assert resolve_href("../a/b.md", "/x/y/z.md") == "/x/a/b.md"
    assert resolve_href("./b.md", "/x/y.md") == "/x/b.md"
    assert resolve_href("../../../b.md", "/x/y.md") == "/b.md"


def test_extract_body_links_skips_external_anchor_and_non_md():
    body = "[a](https://x.com/a.md) [b](#top) [c](mailto:a@b.md) [d](/z/d.csv) [e](e.md#frag) [f](f.md?x=1)"
    assert extract_body_links(body, "/p/q.md") == ["/p/e.md", "/p/f.md"]


def test_relative_parent_links_are_kept(bundle: Path):
    # "../x.md" must not lose its leading dots.
    assert extract_body_links("[x](../x.md)", "/a/b/c.md") == ["/a/x.md"]


def test_nodes_and_ids(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    assert snap.env == "prod"
    assert set(snap.nodes) == {R1, C1, T1, T2, K1, "prod:/misc/AR1.md"}
    t1 = snap.nodes[T1]
    assert (t1.path, t1.bundle, t1.type_family, t1.status) == ("/data/tables/T1.md", "data", "Table", "stable")
    assert t1.tags == ("area:alpha", "group:s")
    assert t1.source_refs == ("store://alpha/S/T1",)
    ar = snap.nodes["prod:/misc/AR1.md"]
    assert (ar.type_family, ar.type_qualifier) == ("Policy.Code", "20240009")


def test_env_override(bundle: Path):
    snap = load_snapshot(bundle, env="cert", alias_rules=RULES)
    assert all(nid.startswith("cert:/") for nid in snap.nodes)
    assert snap.env == "cert"


def test_ignored_files_and_diagnostics(bundle: Path):
    d = load_snapshot(bundle, alias_rules=RULES).diagnostics
    assert d.skipped_no_type == ["/bad.md", "/misc/notype.md"]
    assert [x["path"] for x in d.invalid_frontmatter] == ["/bad.md"]
    assert d.malformed_references == [{"path": "/docs/guides/r1.md", "index": 2}]
    assert d.links_to_non_concept == 1  # r1 -> /index.md
    assert d.concepts == 6
    assert d.md_files == 8  # index.md and .cache are never counted


def test_frontmatter_and_body_edges_merge(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    e = edge(snap, R1, C1)  # declared in frontmatter, absolute body link, and relative body link
    assert e.declared_in == ("frontmatter", "body")
    assert (e.declared_bundle, e.declared_title) == ("docs", "c1")
    assert e.provenance == "process:generate_guides"
    assert e.declared_status == "stable"
    assert e.resolved
    assert e.relation_type == "Guide -> Procedure"
    assert e.relation_alias == "backs"


def test_body_only_edge(bundle: Path):
    e = edge(load_snapshot(bundle, alias_rules=RULES), R1, T2)  # via an href anchor
    assert e.declared_in == ("body",)
    assert e.declared_bundle is None
    assert e.relation_alias is None
    assert e.relation_type == "Guide -> View"


def test_aliases(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    assert edge(snap, C1, T1).relation_alias == "uses"
    assert edge(snap, C1, T2).relation_alias == "uses"
    assert edge(snap, K1, T1).relation_alias == "lands"


def test_unresolved_edges_are_kept_and_flagged(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    missing = edge(snap, R1, "prod:/data/tables/MISSING.md")
    assert not missing.resolved and missing.declared_in == ("frontmatter",)
    assert missing.relation_type is None and missing.relation_alias is None
    gone = edge(snap, R1, "prod:/docs/procedures/gone.md")
    assert not gone.resolved and gone.declared_in == ("body",)
    assert snap.diagnostics.unresolved_edges == 2


def test_no_self_or_external_or_non_md_edges(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    assert all(e.source_id != e.target_id for e in snap.edges)
    assert all("example.com" not in e.target_id and not e.target_id.endswith(".csv") for e in snap.edges)


def test_forward_and_reverse_index(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    assert {snap.edges[i].target_id for i in snap.forward[C1]} == {T1, T2}
    assert {snap.edges[i].source_id for i in snap.reverse[T1]} == {C1, K1}
    assert snap.diagnostics.isolated_nodes == 1  # AR1


def test_kb_hash_is_stable_and_tracks_content(bundle: Path):
    first = load_snapshot(bundle, alias_rules=RULES).kb_hash
    assert load_snapshot(bundle, alias_rules=RULES).kb_hash == first
    assert load_snapshot(bundle, env="cert", alias_rules=RULES).kb_hash != first
    (bundle / "feeds/streams/k1.md").write_text("---\ntype: Stream\ntitle: k1 changed\n---\n")
    assert load_snapshot(bundle, alias_rules=RULES).kb_hash != first


def test_bundle_is_not_modified(bundle: Path):
    before = {p: p.stat().st_mtime_ns for p in bundle.rglob("*") if p.is_file()}
    load_snapshot(bundle, alias_rules=RULES)
    assert before == {p: p.stat().st_mtime_ns for p in bundle.rglob("*") if p.is_file()}


def test_missing_bundle_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(tmp_path / "nope")
