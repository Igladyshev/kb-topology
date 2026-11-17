from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import RULES, _write
from kb_topology.adapter import load_snapshot
from kb_topology.analysis import analyze
from kb_topology.api import create_app
from kb_topology.navigation import EdgeCriteria, Navigator, NodeCriteria
from kb_topology.search import SearchIndex, tokenize
from kb_topology.sessions import SessionStore


def n(name: str) -> str:
    return f"t:/n/{name}.md"


def page(type_: str, title: str, links: list[str] = (), tags: list[str] = (), description: str = "") -> str:
    fm = f"---\ntype: {type_}\ntitle: {title}\n"
    if tags:
        fm += "tags: [" + ", ".join(tags) + "]\n"
    if description:
        fm += f"description: {description}\n"
    return fm + "---\n" + "".join(f"[x](/n/{t}.md)\n" for t in links)


@pytest.fixture
def graph_bundle(tmp_path: Path) -> Path:
    """A->B->C->D, A->E->D, D->F (D is the only articulation point), G->H, and isolated I."""
    root = tmp_path / "t"
    _write(root, "n/A.md", page("Guide", "Invoice Audit Report", ["B", "E"], ["domain:x", "shared"], "Daily invoice audit"))
    _write(root, "n/B.md", page("Procedure", "Invoice Audit Procedure", ["C"], ["shared"]))
    _write(root, "n/C.md", page("Concept", "Account Ledger", ["D"], ["domain:y"]))
    _write(root, "n/D.md", page("Concept", "Account Transactions", ["F"], description="All account movements"))
    _write(root, "n/E.md", page("Hub", "Invoice Bridge", ["D"]))
    _write(root, "n/F.md", page("Concept", "AccountTransaction Staging"))
    _write(root, "m/G.md", page("Concept", "Gamma", ["H"]).replace("/n/H.md", "/m/H.md"))
    _write(root, "m/H.md", page("Concept", "Eta"))
    _write(root, "n/I.md", page("Concept", "Iota"))
    return root


@pytest.fixture
def nav(graph_bundle: Path) -> Navigator:
    return Navigator(analyze(load_snapshot(graph_bundle, alias_rules=RULES)))


def ids(result: dict) -> list[str]:
    return [x["node"]["id"] for x in result["neighbors"]]


def test_topology(nav: Navigator):
    t = nav.topology()
    assert t["components"] == {"count": 3, "largest": 6, "singletons": 1, "top_sizes": [6, 2, 1]}
    assert [p["id"] for p in t["articulation_points"]["top_by_degree"]] == [n("D")]
    assert nav.node_summary(n("D"))["is_articulation_point"]
    assert not nav.node_summary(n("A"))["is_articulation_point"]
    assert nav.node_summary(n("A"))["component_id"] == 0
    assert nav.node_summary(n("I"))["component_id"] == 2


def test_neighbors_directions(nav: Navigator):
    assert ids(nav.neighbors(n("A"), "out")) == [n("B"), n("E")]
    assert ids(nav.neighbors(n("D"), "in")) == [n("C"), n("E")]  # same distance: ordered by title
    assert ids(nav.neighbors(n("D"), "out")) == [n("F")]
    assert set(ids(nav.neighbors(n("D"), "both"))) == {n("C"), n("E"), n("F")}


def test_neighbors_depth_and_distance(nav: Navigator):
    r = nav.neighbors(n("A"), "out", depth=2)
    assert [(x["distance"], x["node"]["id"]) for x in r["neighbors"]] == [(1, n("B")), (1, n("E")), (2, n("C")), (2, n("D"))]
    assert r["total"] == 4 and not r["truncated"]


def test_neighbors_limit_truncates(nav: Navigator):
    r = nav.neighbors(n("A"), "out", depth=3, limit=2)
    assert len(r["neighbors"]) == 2 and r["truncated"] and r["total"] == 5


def test_neighbor_filters(nav: Navigator):
    only_backs = nav.neighbors(n("A"), "out", edge_ok=nav.edge_filter(EdgeCriteria(relation_alias="backs")))
    assert ids(only_backs) == [n("B")]
    tagged = nav.neighbors(n("A"), "out", node_ok=nav.node_filter(NodeCriteria(tag="shared")))
    assert ids(tagged) == [n("B")]
    typed = nav.neighbors(n("A"), "out", node_ok=nav.node_filter(NodeCriteria(type_family="Hub")))
    assert ids(typed) == [n("E")]
    # A filtered-out node is not walked through either.
    blocked = nav.neighbors(n("A"), "out", depth=3, node_ok=nav.node_filter(NodeCriteria(type_family="Concept")))
    assert ids(blocked) == []


def test_shortest_path(nav: Navigator):
    p = nav.shortest_path(n("A"), n("D"))
    assert p["length"] == 2 and [x["id"] for x in p["nodes"]] == [n("A"), n("E"), n("D")]
    assert len(p["edges"]) == 2
    assert nav.shortest_path(n("A"), n("A"))["length"] == 0


def test_shortest_path_direction_and_unreachable(nav: Navigator):
    assert nav.shortest_path(n("D"), n("A"), directed=True) is None
    assert nav.shortest_path(n("D"), n("A"))["length"] == 2
    assert nav.shortest_path(n("A"), n("I")) is None
    assert nav.shortest_path(n("A"), "t:/m/G.md") is None


def test_shortest_path_can_avoid_hubs(nav: Navigator):
    via = nav.via_filter(["Hub"], None)
    p = nav.shortest_path(n("A"), n("D"), via_ok=via)
    assert [x["id"] for x in p["nodes"]] == [n("A"), n("B"), n("C"), n("D")]
    assert nav.via_filter(None, None) is None
    deg = nav.via_filter(None, 1)  # every middle node here has 2+ neighbors
    assert nav.shortest_path(n("A"), n("D"), via_ok=deg) is None


def test_tags(nav: Navigator):
    t = nav.tags()
    assert {"tag": "shared", "count": 2} in t["items"]
    assert t["namespaces"]["domain"] == 2
    assert [i["tag"] for i in nav.tags(namespace="domain")["items"]] == ["domain:x", "domain:y"]
    assert [i["tag"] for i in nav.tags(q="SHAR")["items"]] == ["shared"]


def test_tag_neighbors(nav: Navigator):
    r = nav.tag_neighbors("shared")
    assert r["members"] == 2
    assert {b["node"]["id"]: b["links_from_members"] for b in r["boundary"]} == {n("C"): 1, n("E"): 1}


def test_tokenize_splits_identifiers_and_camel_case():
    assert tokenize("ALPHA_BETA_GAMMA.comp.ledger") == ["alpha", "beta", "gamma", "comp", "ledger"]
    assert tokenize("AccountTransaction v2") == ["account", "transaction", "v2"]


@pytest.fixture
def index(graph_bundle: Path) -> SearchIndex:
    return SearchIndex(load_snapshot(graph_bundle))


def top(index: SearchIndex, q: str, **kw) -> list[str]:
    return [h.node_id for h in index.search(q, **kw)[1]]


def test_search_ranks_title_matches(index: SearchIndex):
    assert set(top(index, "invoice")) == {n("A"), n("B"), n("E")}
    assert top(index, "invoice audit report")[0] == n("A")
    total, hits = index.search("invoice audit report")
    assert hits[0].matched_terms == ["audit", "invoice", "report"]
    assert total >= 2


def test_search_exact_title_bonus(index: SearchIndex):
    assert top(index, "account transactions")[0] == n("D")


def test_search_splits_camel_case_and_matches_prefix(index: SearchIndex):
    assert n("F") in top(index, "transaction")
    assert n("D") in top(index, "accou")  # prefix of "account"
    assert top(index, "wa") == []  # prefixes need 3+ characters


def test_search_description_and_filters(index: SearchIndex):
    assert top(index, "movements") == [n("D")]
    assert top(index, "invoice", node_ok=lambda x: x.type_family == "Hub") == [n("E")]
    assert index.search("invoice", limit=1)[0] >= 3 and len(index.search("invoice", limit=1)[1]) == 1


def test_search_empty_and_unknown(index: SearchIndex):
    assert index.search("") == (0, [])
    assert index.search("zzzznotaword") == (0, [])


# ---- sessions ---------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_session_trail_and_jump_back():
    s = SessionStore(clock=Clock())
    assert s.trail("x") is None and s.jump_back_options("x") is None
    for node in ["A", "B", "C", "B", "D"]:
        s.visit("x", "kb", node)
    assert [v.node_id for v in s.trail("x")] == ["A", "B", "C", "B", "D"]
    options = s.jump_back_options("x")
    assert [o["node_id"] for o in options] == ["B", "C", "A"]  # current node D excluded, no duplicates
    assert options[0]["visits"] == 2
    assert [o["node_id"] for o in s.jump_back_options("x", limit=1)] == ["B"]


def test_session_store_evicts_least_recently_used():
    s = SessionStore(clock=Clock(), max_sessions=2)
    s.visit("a", "kb", "A")
    s.visit("b", "kb", "A")
    s.visit("a", "kb", "B")  # a is now most recent
    s.visit("c", "kb", "A")
    assert s.trail("b") is None and s.trail("a") is not None and s.trail("c") is not None


# ---- API ----------------------------------------------------------------------


@pytest.fixture
def client(graph_bundle: Path):
    snap = load_snapshot(graph_bundle, alias_rules=RULES)
    return TestClient(create_app([snap], sessions=SessionStore(clock=Clock()))), snap.kb_hash


def test_api_neighbors(client):
    c, h = client
    body = c.get(f"/kb/{h}/node/{n('A')}/neighbors", params={"direction": "out", "depth": 2}).json()
    assert [x["node"]["id"] for x in body["neighbors"]] == [n("B"), n("E"), n("C"), n("D")]
    assert body["neighbors"][0]["via_edge"]["relation_alias"] == "backs"
    assert body["neighbors"][0]["via_edge"]["validation_status"] == "validated"
    only = c.get(f"/kb/{h}/node/{n('A')}/neighbors", params={"relation_alias": "backs"}).json()
    assert [x["node"]["id"] for x in only["neighbors"]] == [n("B")]
    assert c.get(f"/kb/{h}/node/{n('A')}/neighbors", params={"depth": 9}).status_code == 422
    assert c.get(f"/kb/{h}/node/t:/n/nope.md/neighbors").status_code == 404


def test_api_path(client):
    c, h = client
    body = c.get(f"/kb/{h}/path", params={"from": n("A"), "to": n("D")}).json()
    assert body["length"] == 2
    avoided = c.get(f"/kb/{h}/path", params={"from": n("A"), "to": n("D"), "avoid_type": "Hub"}).json()
    assert avoided["length"] == 3
    assert c.get(f"/kb/{h}/path", params={"from": n("D"), "to": n("A"), "directed": "true"}).status_code == 404
    assert c.get(f"/kb/{h}/path", params={"from": n("A"), "to": "t:/n/nope.md"}).status_code == 404
    assert c.get(f"/kb/{h}/path", params={"from": n("A")}).status_code == 422


def test_api_search_tags_topology(client):
    c, h = client
    r = c.get(f"/kb/{h}/search", params={"q": "invoice audit report"}).json()
    assert r["items"][0]["node"]["id"] == n("A") and r["total"] >= 2
    assert c.get(f"/kb/{h}/search", params={"q": "invoice", "type_family": "Hub"}).json()["total"] == 1
    assert c.get(f"/kb/{h}/search", params={"q": ""}).status_code == 422
    assert c.get(f"/kb/{h}/tags", params={"namespace": "domain"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/tag/shared/neighbors").json()["members"] == 2
    assert c.get(f"/kb/{h}/tag/nope/neighbors").status_code == 404
    assert c.get(f"/kb/{h}/topology").json()["components"]["count"] == 3


def test_api_session_flow(client):
    c, h = client
    for node in [n("A"), n("B"), n("C"), n("D")]:
        r = c.post("/session/s1/visit", json={"kb_hash": h, "node_id": node, "via": "neighbors"})
        assert r.status_code == 200
    assert r.json()["position"] == 4 and r.json()["visit"]["title"] == "Account Transactions"
    trail = c.get("/session/s1/trail").json()
    assert trail["current"] == n("D") and [i["node_id"] for i in trail["items"]] == [n("A"), n("B"), n("C"), n("D")]
    options = c.get("/session/s1/jump-back-options").json()["items"]
    assert [o["node_id"] for o in options] == [n("C"), n("B"), n("A")]
    # A's neighbors are B (visited) and E (not visited).
    assert options[-1]["unvisited_neighbors"] == 1 and options[0]["title"] == "Account Ledger"


def test_api_session_errors(client):
    c, h = client
    assert c.get("/session/none/trail").status_code == 404
    assert c.get("/session/none/jump-back-options").status_code == 404
    assert c.post("/session/bad id!/visit", json={"kb_hash": h, "node_id": n("A")}).status_code in (404, 422)
    assert c.post("/session/s2/visit", json={"kb_hash": h, "node_id": "t:/n/nope.md"}).status_code == 404
    assert c.post("/session/s2/visit", json={"kb_hash": "bad", "node_id": n("A")}).status_code == 404
