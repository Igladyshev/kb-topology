from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import _write
from kb_topology.adapter import load_snapshot
from kb_topology.analysis import analyze
from kb_topology.api import create_app
from kb_topology.validation import status_for, titles_match

SRC = "dev:/a/src.md"


def concept(name: str, title: str, extra: str = "") -> str:
    return f"---\ntype: Concept\ntitle: {title}\n{extra}---\n"


@pytest.fixture
def fix_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "dev"
    _write(
        root,
        "a/src.md",
        "---\ntype: Pipeline\ntitle: src\nstatus: stable\nreferences:\n"
        "- {bundle: b, id: /b/Target.md, title: Target}\n"
        "- {bundle: wrongbundle, id: /b/Other.md, title: Other}\n"
        "- {bundle: b, id: /b/Third.md, title: stale title}\n"
        "- {bundle: b, id: /b/target.md, title: Target}\n"
        "- {bundle: b, id: /b/Deprecated.md, title: Deprecated}\n"
        "- {bundle: b, id: /b/nowhere_at_all.md, title: x}\n"
        "---\n[body only](/b/BodyOnly.md)\n",
    )
    _write(root, "b/Target.md", concept("Target", "Target"))
    _write(root, "b/Other.md", concept("Other", "Other"))
    _write(root, "b/Third.md", concept("Third", "Third Real"))
    _write(root, "b/Deprecated.md", concept("Deprecated", "Deprecated", "status: deprecated\n"))
    _write(root, "b/BodyOnly.md", concept("BodyOnly", "Body Only"))
    _write(root, "b/Retired.md", concept("Retired", "Retired", "status: deprecated\n"))
    _write(root, "b/RowZero.md", concept("RowZero", "Row Zero", "row_count: 0\n"))
    _write(root, "b/Lonely.md", concept("Lonely", "Lonely"))
    return root


@pytest.fixture
def analysis(fix_bundle: Path):
    return analyze(load_snapshot(fix_bundle), now=datetime(2026, 1, 1, tzinfo=timezone.utc))


def validation(a, target: str):
    return a.validation_by_edge[(SRC, f"dev:/b/{target}.md")]


def fixes_for(a, target: str):
    return a.fixes_by_edge.get((SRC, f"dev:/b/{target}.md"), [])


def test_titles_match_ignores_case_punctuation_and_suffix():
    assert titles_match("gross_gaming_revenue", "Gross Gaming Revenue")
    assert titles_match("Weekly Report", "Weekly Report — SQL")
    assert not titles_match("highest_revenue_monthly_report_bop", "Highest Revenue Report")


def test_status_thresholds():
    assert status_for(0.0) == "validated"
    assert status_for(0.29) == "validated"
    assert status_for(0.3) == "needs-review"
    assert status_for(0.8) == "likely-misleading"


def test_clean_edge_is_validated(analysis):
    v = validation(analysis, "Target")
    assert (v.validation_status, v.risk_score, v.confidence, v.recommended_action) == ("validated", 0.0, 1.0, "none")
    assert v.validated_at == "2026-01-01T00:00:00+00:00"
    assert v.semantic_mismatch is None
    assert fixes_for(analysis, "Target") == []


def test_bundle_mismatch(analysis):
    v = validation(analysis, "Other")
    assert (v.validation_status, v.risk_score, v.recommended_action) == ("needs-review", 0.4, "update-bundle")
    [f] = fixes_for(analysis, "Other")
    assert f.problem == "bundle-mismatch" and f.location == "references[1]"
    assert f.current == {"bundle": "wrongbundle", "id": "/b/Other.md", "title": "Other"}
    assert f.proposed == {"bundle": "b", "id": "/b/Other.md", "title": "Other"}


def test_title_mismatch_is_low_risk_but_gets_a_fix(analysis):
    v = validation(analysis, "Third")
    assert (v.validation_status, v.risk_score) == ("validated", 0.2)
    [f] = fixes_for(analysis, "Third")
    assert f.problem == "title-mismatch" and f.location == "references[2]"
    assert f.proposed["title"] == "Third Real"


def test_deprecated_target_needs_review(analysis):
    v = validation(analysis, "Deprecated")
    assert (v.validation_status, v.risk_score, v.recommended_action) == ("needs-review", 0.35, "review-deprecated-target")
    assert fixes_for(analysis, "Deprecated") == []


def test_broken_target_with_case_fix(analysis):
    v = validation(analysis, "target")
    assert v.validation_status == "likely-misleading" and v.risk_score == 1.0
    [f] = fixes_for(analysis, "target")
    assert (f.problem, f.recommended_action, f.location) == ("broken-target", "retarget", "references[3]")
    assert f.proposed == {"bundle": "b", "id": "/b/Target.md", "title": "Target"}
    assert f.candidates[0].reason == "same path ignoring case" and f.candidates[0].score == 0.98
    assert f.confidence == 0.98


def test_broken_target_without_candidates(analysis):
    [f] = fixes_for(analysis, "nowhere_at_all")
    assert f.proposed is None and f.candidates == []
    assert (f.recommended_action, f.confidence) == ("remove-or-recreate", 0.0)


def test_body_only_edge_gets_promote_proposal(analysis):
    v = validation(analysis, "BodyOnly")
    assert v.validation_status == "validated"
    [f] = fixes_for(analysis, "BodyOnly")
    assert (f.problem, f.location, f.current) == ("body-only", "body", None)
    assert f.proposed == {"bundle": "b", "id": "/b/BodyOnly.md", "title": "Body Only"}
    assert f.recommended_action == "promote-to-frontmatter"


def test_edge_confidence_is_recorded(analysis):
    by_target = {e.target_id: e for e in analysis.snapshot.edges}
    assert by_target["dev:/b/Target.md"].confidence == 1.0
    assert by_target["dev:/b/target.md"].confidence == 0.0


def test_fixes_are_ordered_by_problem(analysis):
    problems = [f.problem for f in analysis.fixes]
    assert problems == sorted(problems, key=["broken-target", "bundle-mismatch", "title-mismatch", "body-only"].index)


def test_retirement_report(analysis):
    by_path = {i.path: i for i in analysis.retirement}
    assert set(by_path) == {"/b/Retired.md", "/b/RowZero.md", "/b/Lonely.md"}
    assert by_path["/b/Retired.md"].category == "retirement-candidate"
    assert by_path["/b/Retired.md"].signals == ["status: deprecated"]
    assert by_path["/b/RowZero.md"].signals == ["row_count: 0"]
    assert by_path["/b/Lonely.md"].category == "isolated-review" and by_path["/b/Lonely.md"].signals == []


def test_summary(analysis):
    s = analysis.summary()
    assert s["validation_status"] == {"validated": 3, "needs-review": 2, "likely-misleading": 2}
    assert s["fix_problems"] == {"broken-target": 2, "bundle-mismatch": 1, "title-mismatch": 1, "body-only": 1}
    assert s["retirement"] == {"isolated-review": 1, "retirement-candidate": 2}


@pytest.fixture
def client(fix_bundle: Path):
    snap = load_snapshot(fix_bundle)
    return TestClient(create_app([snap])), snap.kb_hash


def test_api_validation_listing(client):
    c, h = client
    body = c.get(f"/kb/{h}/links/validation", params={"min_risk": 0.3}).json()
    assert body["total"] == 4
    assert [i["risk_score"] for i in body["items"]] == sorted((i["risk_score"] for i in body["items"]), reverse=True)
    only = c.get(f"/kb/{h}/links/validation", params={"status": "likely-misleading"}).json()
    assert only["total"] == 2
    assert c.get(f"/kb/{h}/links/validation", params={"status": "bogus"}).status_code == 422


def test_api_validate_given_edges(client):
    c, h = client
    payload = {"edges": [
        {"source_id": SRC, "target_id": "dev:/b/target.md"},
        {"source_id": SRC, "target_id": "dev:/b/unknown.md"},
    ]}
    body = c.post(f"/kb/{h}/links/validate", json=payload).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["validation"]["validation_status"] == "likely-misleading"
    assert body["items"][0]["fixes"][0]["proposed"]["id"] == "/b/Target.md"
    assert body["unknown"] == [{"source_id": SRC, "target_id": "dev:/b/unknown.md"}]
    assert c.post(f"/kb/{h}/links/validate", json={"edges": []}).status_code == 422


def test_api_fixes_filters(client):
    c, h = client
    assert c.get(f"/kb/{h}/fixes").json()["total"] == 5
    assert c.get(f"/kb/{h}/fixes", params={"problem": "broken-target"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/fixes", params={"min_confidence": 0.95}).json()["total"] == 1
    assert c.get(f"/kb/{h}/fixes", params={"recommended_action": "retarget"}).json()["total"] == 1
    assert c.get(f"/kb/{h}/fixes", params={"problem": "bogus"}).status_code == 422


def test_api_retirement_and_summary(client):
    c, h = client
    assert c.get(f"/kb/{h}/retirement").json()["total"] == 3
    assert c.get(f"/kb/{h}/retirement", params={"category": "retirement-candidate"}).json()["total"] == 2
    assert c.get(f"/kb/{h}/summary").json()["edges"] == 7
