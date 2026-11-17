"""Tool-owned persistent state: review decisions and session trails (design doc, section 9.1).

One SQLite file in the state directory. It is never inside the KB, so regenerating the KB
cannot destroy it. Review decisions are keyed by edge and carry the content hashes of both
endpoint files, so a decision becomes ``stale`` when either file changes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
DECISIONS = ("approved", "rejected", "corrected")
MIN_SAMPLES = 20
MIN_WEIGHT = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_hash TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    target_hash TEXT,
    decision TEXT NOT NULL,
    correction TEXT,
    note TEXT,
    reviewer TEXT,
    decided_at TEXT NOT NULL,
    risk REAL NOT NULL,
    signals TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_edge ON reviews (source_id, target_id, id);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kb_hash TEXT NOT NULL,
    node_id TEXT NOT NULL,
    at TEXT NOT NULL,
    via TEXT
);
CREATE INDEX IF NOT EXISTS visits_session ON visits (session_id, id);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StateDB:
    """Thin wrapper around a SQLite connection shared by the stores."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            self.path = None
            target = ":memory:"
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.path)
        self.conn = sqlite3.connect(target, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock, self.conn:
            return self.conn.execute(sql, params).fetchall()

    def insert(self, sql: str, params: tuple = ()) -> int:
        with self.lock, self.conn:
            return int(self.conn.execute(sql, params).lastrowid)

    def close(self) -> None:
        self.conn.close()


@dataclass(slots=True)
class ReviewRecord:
    id: int
    kb_hash: str
    source_id: str
    target_id: str
    source_hash: str
    target_hash: str | None
    decision: str
    correction: dict[str, Any] | None
    note: str | None
    reviewer: str | None
    decided_at: str
    risk: float
    signals: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "decision": self.decision,
            "correction": self.correction,
            "note": self.note,
            "reviewer": self.reviewer,
            "decided_at": self.decided_at,
            "risk_at_decision": self.risk,
            "signals_at_decision": self.signals,
        }


def _record(row) -> ReviewRecord:
    return ReviewRecord(
        id=row["id"], kb_hash=row["kb_hash"], source_id=row["source_id"], target_id=row["target_id"],
        source_hash=row["source_hash"], target_hash=row["target_hash"], decision=row["decision"],
        correction=json.loads(row["correction"]) if row["correction"] else None, note=row["note"],
        reviewer=row["reviewer"], decided_at=row["decided_at"], risk=row["risk"], signals=json.loads(row["signals"]),
    )


class ReviewStore:
    """Append-only review decisions. The latest decision for an edge is the current one."""

    def __init__(self, db: StateDB, clock: Callable[[], datetime] = _utc_now) -> None:
        self.db = db
        self._clock = clock

    def record(
        self,
        kb_hash: str,
        source_id: str,
        target_id: str,
        source_hash: str,
        target_hash: str | None,
        decision: str,
        risk: float,
        signals: list[str],
        correction: dict[str, Any] | None = None,
        note: str | None = None,
        reviewer: str | None = None,
    ) -> ReviewRecord:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if decision == "corrected" and not correction:
            raise ValueError("a 'corrected' decision needs a correction")
        row_id = self.db.insert(
            "INSERT INTO reviews (kb_hash, source_id, target_id, source_hash, target_hash, decision, correction,"
            " note, reviewer, decided_at, risk, signals) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kb_hash, source_id, target_id, source_hash, target_hash, decision,
             json.dumps(correction) if correction else None, note, reviewer,
             self._clock().isoformat(timespec="seconds"), risk, json.dumps(signals)),
        )
        return _record(self.db.execute("SELECT * FROM reviews WHERE id = ?", (row_id,))[0])

    def latest(self, env_prefix: str | None = None) -> dict[tuple[str, str], ReviewRecord]:
        """Current decision per edge. ``env_prefix`` (for example "prod:") limits to one environment."""
        rows = self.db.execute(
            "SELECT r.* FROM reviews r JOIN (SELECT MAX(id) AS id FROM reviews GROUP BY source_id, target_id) m ON r.id = m.id"
        )
        out = {(r["source_id"], r["target_id"]): _record(r) for r in rows}
        if env_prefix:
            out = {k: v for k, v in out.items() if k[0].startswith(env_prefix)}
        return out

    def history(self, source_id: str, target_id: str) -> list[ReviewRecord]:
        rows = self.db.execute("SELECT * FROM reviews WHERE source_id = ? AND target_id = ? ORDER BY id", (source_id, target_id))
        return [_record(r) for r in rows]


def calibration(
    decisions: dict[tuple[str, str], ReviewRecord],
    weights: dict[str, float],
    min_samples: int = MIN_SAMPLES,
) -> dict[str, Any]:
    """Per-signal precision from review decisions, and the weight each signal would get.

    A signal "fired" on a decided edge if it was in ``signals_at_decision``. The edge is
    "confirmed" when the reviewer rejected or corrected it, "dismissed" when approved. A weight
    is read as the probability that a firing signal marks a real problem, so the suggested
    weight is the observed precision once ``min_samples`` decisions exist. Nothing is applied
    automatically.
    """
    stats = {name: {"fired": 0, "confirmed": 0, "dismissed": 0} for name in weights}
    missed = 0
    for rec in decisions.values():
        bad = rec.decision in ("rejected", "corrected")
        if bad and not rec.signals:
            missed += 1
        for name in rec.signals:
            if name in stats:
                stats[name]["fired"] += 1
                stats[name]["confirmed" if bad else "dismissed"] += 1
    signals = {}
    for name, s in stats.items():
        precision = s["confirmed"] / s["fired"] if s["fired"] else None
        enough = s["fired"] >= min_samples and precision is not None
        signals[name] = {
            **s,
            "precision": round(precision, 4) if precision is not None else None,
            "current_weight": weights[name],
            "suggested_weight": round(max(MIN_WEIGHT, precision), 2) if enough else weights[name],
            "enough_samples": enough,
        }
    return {
        "decisions": len(decisions),
        "min_samples": min_samples,
        "missed_by_validator": missed,  # rejected or corrected edges that no signal had flagged
        "signals": signals,
    }
