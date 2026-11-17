import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import _write
from kb_topology.__main__ import run_calibration
from kb_topology.adapter import load_snapshot
from kb_topology.analysis import analyze
from kb_topology.api import create_app
from kb_topology.sessions import SessionStore
from kb_topology.state import ReviewRecord, ReviewStore, StateDB, calibration
from kb_topology.validation import DEFAULT_WEIGHTS, load_weights

SRC = "r:/a/src.md"
OK, DEP, GONE = "r:/b/Target.md", "r:/b/Deprecated.md", "r:/b/missing.md"


def concept(title: str, extra: str = "") -> str:
    return f"---\ntype: Concept\ntitle: {title}\n{extra}---\n"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    _write(
        root, "a/src.md",
        "---\ntype: Pipeline\ntitle: src\nstatus: stable\nreferences:\n"
        "- {bundle: b, id: /b/Target.md, title: Target}\n"
        "- {bundle: b, id: /b/Deprecated.md, title: Deprecated}\n"
        "- {bundle: b, id: /b/missing.md, title: Missing}\n---\n",
    )
    _write(root, "b/Target.md", concept("Target"))
    _write(root, "b/Deprecated.md", concept("Deprecated", "status: deprecated\n"))
    return root


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def client_for(bundle: Path, state: Path) -> tuple[TestClient, str]:
    snap = load_snapshot(bundle)
    return TestClient(create_app([snap], state_dir=state)), snap.kb_hash


# ---- store ---------------------------------------------------------------------------


def test_store_keeps_history_and_returns_latest():
    store = ReviewStore(StateDB(), clock=Clock())
    first = store.record("h", SRC, DEP, "s1", "t1", "approved", 0.35, ["deprecated_target"], reviewer="ana")
    second = store.record("h", SRC, DEP, "s1", "t1", "rejected", 0.35, ["deprecated_target"], note="really deprecated")
    latest = store.latest()
    assert list(latest) == [(SRC, DEP)] and latest[(SRC, DEP)].id == second.id
    assert [r.decision for r in store.history(SRC, DEP)] == ["approved", "rejected"]
    assert first.signals == ["deprecated_target"] and first.reviewer == "ana"
    assert first.decided_at == "2026-01-01T00:00:01+00:00"


def test_store_validates_decisions_and_scopes_by_env():
    store = ReviewStore(StateDB())
    with pytest.raises(ValueError):
        store.record("h", SRC, DEP, "s", None, "maybe", 0.1, [])
    with pytest.raises(ValueError):
        store.record("h", SRC, DEP, "s", None, "corrected", 0.1, [])
    store.record("h", "prod:/x.md", "prod:/y.md", "s", None, "approved", 0.1, [])
    store.record("h", "cert:/x.md", "cert:/y.md", "s", None, "approved", 0.1, [])
    assert len(store.latest()) == 2 and list(store.latest("cert:")) == [("cert:/x.md", "cert:/y.md")]


def test_state_survives_reopening(tmp_path: Path):
    db = StateDB(tmp_path / "s" / "state.db")
    ReviewStore(db).record("h", SRC, DEP, "s", None, "approved", 0.3, [])
    db.close()
    assert len(ReviewStore(StateDB(tmp_path / "s" / "state.db")).latest()) == 1


# ---- calibration ------------------------------------------------------------------------


def rec(i: int, decision: str, signals: list[str]) -> ReviewRecord:
    return ReviewRecord(i, "h", f"p:/s{i}.md", f"p:/t{i}.md", "s", None, decision, None, None, None, "t", 0.5, signals)


def test_calibration_uses_precision_once_enough_samples():
    decisions = {}
    for i in range(30):  # deprecated_target: 6 of 30 are real problems
        decisions[(f"p:/s{i}.md", "t")] = rec(i, "rejected" if i < 6 else "approved", ["deprecated_target"])
    for i in range(30, 33):  # title_mismatch: only 3 decisions
        decisions[(f"p:/s{i}.md", "t")] = rec(i, "corrected", ["title_mismatch"])
    decisions[("p:/s99.md", "t")] = rec(99, "rejected", [])  # a real problem no signal flagged
    out = calibration(decisions, dict(DEFAULT_WEIGHTS), min_samples=20)
    dep = out["signals"]["deprecated_target"]
    assert (dep["fired"], dep["confirmed"], dep["dismissed"], dep["precision"]) == (30, 6, 24, 0.2)
    assert dep["suggested_weight"] == 0.2 and dep["current_weight"] == 0.35 and dep["enough_samples"]
    title = out["signals"]["title_mismatch"]
    assert title["precision"] == 1.0 and not title["enough_samples"] and title["suggested_weight"] == DEFAULT_WEIGHTS["title_mismatch"]
    assert out["signals"]["broken_target"]["precision"] is None
    assert out["missed_by_validator"] == 1 and out["decisions"] == 34


def test_calibration_never_suggests_a_zero_weight():
    decisions = {(f"p:/s{i}.md", "t"): rec(i, "approved", ["discontinuity"]) for i in range(25)}
    assert calibration(decisions, dict(DEFAULT_WEIGHTS))["signals"]["discontinuity"]["suggested_weight"] == 0.05


def test_weights_file_overrides_defaults_and_ignores_bad_values(tmp_path: Path):
    assert load_weights(None) == DEFAULT_WEIGHTS and load_weights(tmp_path) == DEFAULT_WEIGHTS
    (tmp_path / "weights.json").write_text(json.dumps({"deprecated_target": 0.1, "title_mismatch": 7, "nope": 0.5, "discontinuity": "x"}))
    w = load_weights(tmp_path)
    assert w["deprecated_target"] == 0.1 and w["title_mismatch"] == DEFAULT_WEIGHTS["title_mismatch"]
    assert set(w) == set(DEFAULT_WEIGHTS)


def test_lower_weight_changes_validation_status(bundle: Path):
    snap = load_snapshot(bundle)
    assert analyze(snap).validation_by_edge[(SRC, DEP)].validation_status == "needs-review"
    low = analyze(load_snapshot(bundle), weights={**DEFAULT_WEIGHTS, "deprecated_target": 0.1})
    assert low.validation_by_edge[(SRC, DEP)].validation_status == "validated"


def test_calibrate_command_writes_weights(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    store = ReviewStore(db)
    for i in range(25):
        store.record("h", f"prod:/s{i}.md", "prod:/t.md", "s", None, "approved", 0.4, ["deprecated_target"])
    db.close()
    dry = run_calibration(tmp_path, "prod", apply=False, min_samples=20)
    assert not (tmp_path / "weights.json").exists() and dry["signals"]["deprecated_target"]["suggested_weight"] == 0.05
    run_calibration(tmp_path, "prod", apply=True, min_samples=20)
    assert load_weights(tmp_path)["deprecated_target"] == 0.05
    assert load_weights(tmp_path)["broken_target"] == 1.0
    assert run_calibration(tmp_path, "cert", apply=False, min_samples=20)["decisions"] == 0


# ---- API ----------------------------------------------------------------------------------


def review_body(target: str, decision: str, **extra) -> dict:
    return {"source_id": SRC, "target_id": target, "decision": decision, **extra}


def test_api_review_flow(bundle: Path, tmp_path: Path):
    c, h = client_for(bundle, tmp_path / "state")
    queue = c.get(f"/kb/{h}/review/queue").json()
    assert [i["validation"]["target_id"] for i in queue["items"]] == [GONE, DEP]  # highest risk first
    assert queue["items"][0]["fixes"][0]["problem"] == "broken-target" and queue["items"][0]["review"] is None

    r = c.post(f"/kb/{h}/links/review", json=review_body(DEP, "approved", reviewer="ana", note="still used"))
    assert r.status_code == 201
    assert r.json()["review"]["state"] == "fresh" and r.json()["review"]["signals_at_decision"] == ["deprecated_target"]
    assert [i["validation"]["target_id"] for i in c.get(f"/kb/{h}/review/queue").json()["items"]] == [GONE]

    fix = {"bundle": "b", "id": "/b/Target.md", "title": "Target"}
    assert c.post(f"/kb/{h}/links/review", json=review_body(GONE, "corrected", correction=fix)).status_code == 201
    assert c.get(f"/kb/{h}/review/queue").json()["total"] == 0

    decisions = c.get(f"/kb/{h}/review/decisions").json()
    assert decisions["total"] == 2 and decisions["items"][0]["decision"] == "corrected"
    assert c.get(f"/kb/{h}/review/decisions", params={"decision": "approved"}).json()["total"] == 1
    listed = c.get(f"/kb/{h}/links/validation", params={"min_risk": 0.3}).json()["items"]
    assert {i["target_id"]: i["review"]["decision"] for i in listed} == {GONE: "corrected", DEP: "approved"}
    both = c.post(f"/kb/{h}/links/validate", json={"edges": [{"source_id": SRC, "target_id": DEP}]}).json()
    assert both["items"][0]["review"]["reviewer"] == "ana"


def test_api_review_errors(bundle: Path, tmp_path: Path):
    c, h = client_for(bundle, tmp_path / "state")
    assert c.post(f"/kb/{h}/links/review", json=review_body("r:/b/nope.md", "approved")).status_code == 404
    assert c.post(f"/kb/{h}/links/review", json=review_body(DEP, "corrected")).status_code == 422
    assert c.post(f"/kb/{h}/links/review", json=review_body(DEP, "maybe")).status_code == 422
    assert c.get(f"/kb/{h}/review/queue", params={"status": "validated"}).status_code == 422


def test_decisions_persist_and_go_stale_when_a_file_changes(bundle: Path, tmp_path: Path):
    state = tmp_path / "state"
    c, h = client_for(bundle, state)
    c.post(f"/kb/{h}/links/review", json=review_body(DEP, "approved"))
    c.post(f"/kb/{h}/links/review", json=review_body(GONE, "rejected"))

    c2, h2 = client_for(bundle, state)  # new process, same bundle: decisions are still current
    assert h2 == h and c2.get(f"/kb/{h2}/review/queue").json()["total"] == 0
    assert c2.get(f"/kb/{h2}/review/decisions", params={"state": "fresh"}).json()["total"] == 2

    (bundle / "b/Deprecated.md").write_text(concept("Deprecated, updated", "status: deprecated\n"))
    _write(bundle, "b/missing.md", concept("Missing now exists"))
    c3, h3 = client_for(bundle, state)  # the KB was regenerated
    assert h3 != h
    stale = c3.get(f"/kb/{h3}/review/decisions", params={"state": "stale"}).json()
    assert stale["total"] == 2
    queue = c3.get(f"/kb/{h3}/review/queue").json()
    assert [i["validation"]["target_id"] for i in queue["items"]] == [DEP]  # the new file is fine, the changed one returns
    assert queue["items"][0]["review"]["state"] == "stale"


def test_api_history_and_calibration(bundle: Path, tmp_path: Path):
    c, h = client_for(bundle, tmp_path / "state")
    c.post(f"/kb/{h}/links/review", json=review_body(DEP, "approved"))
    c.post(f"/kb/{h}/links/review", json=review_body(DEP, "rejected"))
    history = c.get(f"/kb/{h}/review/history", params={"source_id": SRC, "target_id": DEP}).json()["items"]
    assert [x["decision"] for x in history] == ["approved", "rejected"]
    cal = c.get(f"/kb/{h}/review/calibration", params={"min_samples": 1}).json()
    dep = cal["signals"]["deprecated_target"]
    assert dep["fired"] == 1 and dep["confirmed"] == 1 and dep["suggested_weight"] == 1.0
    assert cal["decisions"] == 1  # only the latest decision per edge counts


def test_server_uses_weights_file(bundle: Path, tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "weights.json").write_text(json.dumps({"deprecated_target": 0.1}))
    c, h = client_for(bundle, state)
    deps = [i for i in c.get(f"/kb/{h}/links/validation").json()["items"] if i["target_id"] == DEP]
    assert deps[0]["validation_status"] == "validated"


# ---- sessions on disk --------------------------------------------------------------------------


def test_sessions_survive_restart_and_evict_cleanly(tmp_path: Path):
    path = tmp_path / "state.db"
    s1 = SessionStore(clock=Clock(), db=StateDB(path), max_sessions=2)
    for node in ["A", "B", "C"]:
        s1.visit("one", "kb", node, via="neighbors")
    s1.visit("two", "kb", "X")
    s1.visit("three", "kb", "Y")  # evicts "one", the least recently used

    s2 = SessionStore(clock=Clock(), db=StateDB(path), max_sessions=2)
    assert s2.trail("one") is None
    assert [v.node_id for v in s2.trail("two")] == ["X"] and [v.node_id for v in s2.trail("three")] == ["Y"]
    s2.visit("two", "kb", "Z")
    assert [v.node_id for v in SessionStore(db=StateDB(path)).trail("two")] == ["X", "Z"]
    assert SessionStore(db=StateDB(path)).trail("two")[0].via is None


def test_api_sessions_persist_with_state_dir(bundle: Path, tmp_path: Path):
    state = tmp_path / "state"
    c, h = client_for(bundle, state)
    c.post("/session/s1/visit", json={"kb_hash": h, "node_id": OK})
    c.post("/session/s1/visit", json={"kb_hash": h, "node_id": DEP})
    c2, _ = client_for(bundle, state)
    trail = c2.get("/session/s1/trail").json()
    assert [i["node_id"] for i in trail["items"]] == [OK, DEP] and trail["current"] == DEP
