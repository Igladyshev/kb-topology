from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import RULES
from kb_topology.adapter import load_snapshot
from kb_topology.api import create_app

C1 = "prod:/docs/procedures/c1.md"
T1 = "prod:/data/tables/T1.md"


@pytest.fixture
def client(bundle: Path):
    snap = load_snapshot(bundle, alias_rules=RULES)
    return TestClient(create_app([snap])), snap.kb_hash


def test_list_snapshots(client):
    c, h = client
    body = c.get("/kb").json()
    assert body[0]["kb_hash"] == h and body[0]["env"] == "prod" and body[0]["nodes"] == 6


def test_all_links(client):
    c, h = client
    body = c.get(f"/kb/{h}/links").json()
    assert body["total"] == len(body["items"]) == 7
    assert {"source_id", "target_id", "declared_in", "relation_alias", "resolved"} <= set(body["items"][0])


def test_links_of_node_by_direction(client):
    c, h = client
    out = c.get(f"/kb/{h}/links", params={"node": C1, "direction": "out"}).json()
    assert {e["target_id"] for e in out["items"]} == {T1, "prod:/data/tables/T2.md"}
    inc = c.get(f"/kb/{h}/links", params={"node": C1, "direction": "in"}).json()
    assert [e["source_id"] for e in inc["items"]] == ["prod:/docs/guides/r1.md"]
    both = c.get(f"/kb/{h}/links", params={"node": C1}).json()
    assert both["total"] == 3


def test_filters(client):
    c, h = client
    assert c.get(f"/kb/{h}/links", params={"relation_alias": "uses"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/links", params={"resolved": "false"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/links", params={"declared_in": "frontmatter"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/links", params={"relation_type": "Stream -> Table"}).json()["total"] == 1


def test_paging(client):
    c, h = client
    page = c.get(f"/kb/{h}/links", params={"limit": 3, "offset": 6}).json()
    assert page["total"] == 7 and len(page["items"]) == 1


def test_errors(client):
    c, h = client
    assert c.get("/kb/deadbeef/links").status_code == 404
    assert c.get(f"/kb/{h}/links", params={"node": "prod:/nope.md"}).status_code == 404
    assert c.get(f"/kb/{h}/links", params={"direction": "sideways"}).status_code == 422
    assert c.get(f"/kb/{h}/links", params={"limit": 0}).status_code == 422


def test_diagnostics(client):
    c, h = client
    d = c.get(f"/kb/{h}/diagnostics").json()
    assert d["concepts"] == 6 and d["unresolved_edges"] == 2
    assert d["skipped_no_type"] == ["/bad.md", "/misc/notype.md"]
