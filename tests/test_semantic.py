import hashlib
import math
import re
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import _write
from kb_topology.adapter import load_snapshot
from kb_topology.analysis import analyze
from kb_topology.api import create_app
from kb_topology.embeddings import DEFAULT_MODEL_DIR, EmbeddingStore, MiniLMProvider, resolve_model_dir
from kb_topology.navigation import Navigator
from kb_topology.search import SearchIndex
from kb_topology.semantic import SemanticHit, SemanticService, body_hints, embedding_text


class FakeProvider:
    """Hashed bag of words. Deterministic, no model files."""

    model_id = "fake-64"
    dim = 64

    def __init__(self) -> None:
        self.calls = 0
        self.texts_embedded = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        self.texts_embedded += len(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norm, 1e-12)


def page(title: str, links: list[str] = (), body: str = "", type_: str = "Concept") -> str:
    return f"---\ntype: {type_}\ntitle: {title}\n---\n{body}" + "".join(f"[x](/c/{t}.md)\n" for t in links)


@pytest.fixture
def chain_bundle(tmp_path: Path) -> Path:
    """P-Q-R-S-T in a line. R also links X1 and X2, so R has 4 neighbors."""
    root = tmp_path / "s"
    _write(root, "c/P.md", page("alpha banana", ["Q"]))
    _write(root, "c/Q.md", page("gamma", ["R"]))
    _write(root, "c/R.md", page("delta", ["S", "X1", "X2"]))
    _write(root, "c/S.md", page("epsilon", ["T"]))
    _write(root, "c/T.md", page("zeta"))
    _write(root, "c/X1.md", page("xray one"))
    _write(root, "c/X2.md", page("xray two"))
    return root


def make_service(bundle: Path, state: Path, provider=None) -> SemanticService:
    snap = load_snapshot(bundle)
    provider = provider or FakeProvider()
    nav = Navigator(analyze(snap))
    return SemanticService(snap, provider, EmbeddingStore(state, provider.model_id), nav, SearchIndex(snap))


def n(name: str) -> str:
    return f"s:/c/{name}.md"


# ---- embedding text ------------------------------------------------------------


def test_body_hints_extract_columns_and_headings():
    text = (
        "---\ntype: Table\ntitle: T\n---\n# Properties[^source-note]\n# Schema\n"
        "| Column | Type |\n|---|---|\n| `UUID` | `VARCHAR` |\n| `PLAYER_ID` | `VARCHAR` |\n| `UUID` | dup |\n"
        "## Lineage notes\n"
    )
    columns, headings = body_hints(text)
    assert columns == ["UUID", "PLAYER_ID"]
    assert headings == ["Lineage notes"]  # generic headings and footnote markers are dropped


def test_embedding_text_puts_identity_first(tmp_path: Path):
    root = tmp_path / "s"
    _write(
        root,
        "db/alpha/tables/Ledger.md",
        "---\ntype: Table\ntitle: alpha.comp.ledger\ntags: ['group:comp']\ndescription: Ledger table\n---\n"
        "| `UUID` | `VARCHAR` |\n| `ISSUED` | `DATE` |\n",
    )
    node = next(iter(load_snapshot(root).nodes.values()))
    text = embedding_text(node, root)
    assert text.startswith("alpha.comp.ledger. Table. group comp. Ledger table.")
    assert "columns: UUID, ISSUED" in text
    assert embedding_text(node, None).endswith("db alpha tables Ledger")  # no body hints without a root


# ---- store -----------------------------------------------------------------------


def test_store_embeds_only_missing_texts_and_persists(tmp_path: Path):
    provider = FakeProvider()
    store = EmbeddingStore(tmp_path, provider.model_id)
    first = store.matrix(provider, ["one two", "three", "one two"])
    assert provider.texts_embedded == 2 and first.shape == (3, 64) and np.allclose(first[0], first[2])
    again = store.matrix(provider, ["one two", "three"])
    assert provider.texts_embedded == 2  # nothing new embedded
    reloaded = EmbeddingStore(tmp_path, provider.model_id)
    assert np.allclose(reloaded.matrix(provider, ["three", "one two"]), again[::-1])
    assert provider.texts_embedded == 2
    store.matrix(provider, ["one two", "four"])
    assert provider.texts_embedded == 3


def test_store_is_keyed_by_model(tmp_path: Path):
    a = EmbeddingStore(tmp_path, "m1")
    a.matrix(FakeProvider(), ["x"])
    assert not EmbeddingStore(tmp_path, "m2").vectors


# ---- highlighting --------------------------------------------------------------------


def test_highlight_decays_with_graph_distance(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    hits = svc.search("alpha banana", top_k=1, expand_hops=2, alpha=0.7, min_highlight=0.0)
    direct = hits[0]
    assert direct.node_id == n("P") and direct.match_type == "direct" and direct.hop_distance == 0
    assert direct.highlight_score == direct.match_score
    expanded = {h.node_id: h for h in hits[1:]}
    assert set(expanded) == {n("Q"), n("R")}  # two hops from P, nothing further
    assert expanded[n("Q")].highlight_score == pytest.approx(direct.match_score * math.exp(-0.7 * 1))
    assert expanded[n("R")].highlight_score == pytest.approx(direct.match_score * math.exp(-0.7 * 2))
    assert expanded[n("Q")].evidence == {"matched_from": n("P")}
    assert all(h.match_type == "expanded" for h in expanded.values())


def test_highlight_takes_the_maximum_over_matches(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    p = SemanticHit(n("P"), 0.9, 0.0, 0.8, 0, 0.8, "direct")
    t = SemanticHit(n("T"), 0.7, 0.0, 0.6, 0, 0.6, "direct")
    out = {h.node_id: h for h in svc._expand([p, t], hops=2, alpha=1.0, max_via_degree=None, node_ok=None)}
    assert out[n("R")].highlight_score == pytest.approx(0.8 * math.exp(-2))  # from P, beats T's 0.6*exp(-2)
    assert out[n("R")].evidence["matched_from"] == n("P")
    assert out[n("S")].highlight_score == pytest.approx(0.6 * math.exp(-1))
    assert out[n("Q")].highlight_score == pytest.approx(0.8 * math.exp(-1))


def test_expansion_stops_at_hubs(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    p = SemanticHit(n("P"), 0.9, 0.0, 0.8, 0, 0.8, "direct")
    free = {h.node_id for h in svc._expand([p], 3, 0.7, None, None)}
    guarded = {h.node_id for h in svc._expand([p], 3, 0.7, 2, None)}
    assert free == {n("Q"), n("R"), n("S"), n("X1"), n("X2")}
    assert guarded == {n("Q"), n("R")}  # R has 4 neighbors: reached, not passed through


def test_min_highlight_and_max_expanded(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    base = dict(top_k=1, expand_hops=3, alpha=0.7, max_via_degree=None)
    all_hits = svc.search("alpha banana", min_highlight=0.0, **base)
    assert len(all_hits) == 6
    assert len(svc.search("alpha banana", min_highlight=0.0, max_expanded=2, **base)) == 3
    strict = svc.search("alpha banana", min_highlight=all_hits[1].highlight_score + 1e-6, **base)
    assert [h.node_id for h in strict] == [n("P")]
    assert len(svc.search("alpha banana", expand_hops=0, top_k=1)) == 1


def test_keyword_score_lifts_exact_name_matches(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    hit = svc.search("alpha banana", top_k=1)[0]
    assert hit.keyword_score == 1.0
    assert hit.match_score == pytest.approx(0.7 * max(hit.semantic_score, 0) + 0.3 * 1.0)
    pure = svc.search("alpha banana", top_k=1, keyword_weight=0.0)[0]
    assert pure.match_score == pytest.approx(max(pure.semantic_score, 0))


def test_filters_apply_to_direct_and_expanded(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    svc.build()
    hits = svc.search("alpha banana", top_k=3, expand_hops=3, min_highlight=0.0, node_ok=lambda x: x.title != "alpha banana")
    assert n("P") not in {h.node_id for h in hits}


def test_search_requires_ready_index(chain_bundle: Path, tmp_path: Path):
    svc = make_service(chain_bundle, tmp_path)
    with pytest.raises(RuntimeError):
        svc.search("alpha")


def test_build_failure_is_reported(chain_bundle: Path, tmp_path: Path):
    class Broken(FakeProvider):
        def embed(self, texts):
            raise ValueError("boom")

    svc = make_service(chain_bundle, tmp_path, Broken())
    svc.build()
    assert svc.status()["state"] == "failed" and "boom" in svc.status()["error"]


# ---- API ----------------------------------------------------------------------------------


@pytest.fixture
def client(chain_bundle: Path, tmp_path: Path):
    snap = load_snapshot(chain_bundle)
    app = create_app([snap], provider=FakeProvider(), state_dir=tmp_path / "state", semantic_sync=True)
    return TestClient(app), snap.kb_hash


def test_api_semantic_search(client):
    c, h = client
    body = c.get(f"/kb/{h}/search/semantic", params={"q": "alpha banana", "top_k": 1, "expand_hops": 2, "min_highlight": 0}).json()
    assert body["model"] == "fake-64" and body["direct"] == 1
    direct, *expanded = body["items"]
    assert direct["node_id"] == n("P") and direct["match_type"] == "direct" and direct["node"]["title"] == "alpha banana"
    assert [(e["node_id"], e["hop_distance"]) for e in expanded] == [(n("Q"), 1), (n("R"), 2)]
    assert expanded[0]["highlight_score"] > expanded[1]["highlight_score"]


def test_api_semantic_validation_and_status(client):
    c, h = client
    assert c.get(f"/kb/{h}/search/semantic", params={"q": ""}).status_code == 422
    assert c.get(f"/kb/{h}/search/semantic", params={"q": "x", "expand_hops": 9}).status_code == 422
    assert c.get(f"/kb/{h}/search/semantic", params={"q": "x", "alpha": 0}).status_code == 422
    status = c.get(f"/kb/{h}/search/semantic/status").json()
    assert status["state"] == "ready" and status["done"] == status["total"] == 7
    assert c.get(f"/kb/{h}/summary").json()["semantic"]["state"] == "ready"


def test_api_semantic_disabled_and_building(chain_bundle: Path, tmp_path: Path):
    snap = load_snapshot(chain_bundle)
    off = TestClient(create_app([snap]))
    r = off.get(f"/kb/{snap.kb_hash}/search/semantic", params={"q": "alpha"})
    assert r.status_code == 503 and r.json()["detail"]["state"] == "disabled"
    pending = TestClient(create_app([snap], provider=FakeProvider(), state_dir=tmp_path))  # build not started
    r = pending.get(f"/kb/{snap.kb_hash}/search/semantic", params={"q": "alpha"})
    assert r.status_code == 503 and r.json()["detail"]["state"] == "pending"


def test_api_builds_in_background_on_startup(chain_bundle: Path, tmp_path: Path):
    snap = load_snapshot(chain_bundle)
    app = create_app([snap], provider=FakeProvider(), state_dir=tmp_path)
    with TestClient(app) as c:  # entering the client runs the startup hook
        for _ in range(100):
            if c.get(f"/kb/{snap.kb_hash}/search/semantic/status").json()["state"] == "ready":
                break
            import time

            time.sleep(0.05)
        assert c.get(f"/kb/{snap.kb_hash}/search/semantic", params={"q": "alpha banana"}).status_code == 200


# ---- real model ----------------------------------------------------------------------------


@pytest.mark.skipif(not (resolve_model_dir() / "model.safetensors").is_file(), reason="local embedding model not available")
def test_minilm_matches_reference_and_orders_similarity():
    provider = MiniLMProvider(resolve_model_dir())
    v = provider.embed(["invoice payment reconciliation", "invoice balance", "weather forecast report", "The cat sits on the mat"])
    # Reference: the same encoder run with torch + transformers (maximum difference 1.5e-7).
    assert np.allclose(v[0, :6], [-0.11524, 0.07485, -0.00073, -0.05161, -0.15781, -0.04894], atol=1e-4)
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    sims = v @ v.T
    assert sims[0, 1] > 0.6 and sims[0, 2] < 0.2 and sims[0, 3] < 0.2


def test_missing_model_dir_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        MiniLMProvider(tmp_path)
    assert DEFAULT_MODEL_DIR.name == "all-MiniLM-L6-v2"
