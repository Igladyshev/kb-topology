"""Session trails for backtracking (design doc, section 8).

In memory for now. Persistence arrives with the tool-owned store (design 9.1).
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .state import StateDB

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SESSIONS = 1000
MAX_TRAIL = 10_000


@dataclass(frozen=True, slots=True)
class Visit:
    kb_hash: str
    node_id: str
    at: str  # ISO timestamp
    via: str | None = None  # how the user got here, e.g. "search", "neighbors", "jump-back"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    def __init__(
        self,
        clock: Callable[[], datetime] = _utc_now,
        max_sessions: int = MAX_SESSIONS,
        db: StateDB | None = None,
    ) -> None:
        self._clock = clock
        self._max = max_sessions
        self._db = db
        self._sessions: OrderedDict[str, list[Visit]] = OrderedDict()
        self._lock = threading.Lock()
        if db is not None:
            self._load()

    def _load(self) -> None:
        """Restore trails saved by an earlier run. Sessions are ordered by their last visit."""
        assert self._db is not None
        trails: dict[str, list[Visit]] = {}
        last: dict[str, int] = {}
        for row in self._db.execute("SELECT id, session_id, kb_hash, node_id, at, via FROM visits ORDER BY id"):
            trails.setdefault(row["session_id"], []).append(Visit(row["kb_hash"], row["node_id"], row["at"], row["via"]))
            last[row["session_id"]] = row["id"]
        for sid in sorted(trails, key=last.__getitem__)[-self._max :]:
            self._sessions[sid] = trails[sid][-MAX_TRAIL:]

    def visit(self, session_id: str, kb_hash: str, node_id: str, via: str | None = None) -> Visit:
        v = Visit(kb_hash, node_id, self._clock().isoformat(timespec="seconds"), via)
        with self._lock:
            trail = self._sessions.setdefault(session_id, [])
            self._sessions.move_to_end(session_id)
            trail.append(v)
            if len(trail) > MAX_TRAIL:
                del trail[0 : len(trail) - MAX_TRAIL]
            evicted = []
            while len(self._sessions) > self._max:
                evicted.append(self._sessions.popitem(last=False)[0])
        if self._db is not None:
            self._db.insert(
                "INSERT INTO visits (session_id, kb_hash, node_id, at, via) VALUES (?,?,?,?,?)",
                (session_id, kb_hash, node_id, v.at, via),
            )
            for sid in evicted:
                self._db.execute("DELETE FROM visits WHERE session_id = ?", (sid,))
        return v

    def trail(self, session_id: str) -> list[Visit] | None:
        with self._lock:
            trail = self._sessions.get(session_id)
            return list(trail) if trail is not None else None

    def jump_back_options(self, session_id: str, limit: int = 10) -> list[dict] | None:
        """Previously visited nodes, most recent first, excluding the current one."""
        trail = self.trail(session_id)
        if trail is None:
            return None
        current = trail[-1]
        counts: dict[tuple[str, str], int] = {}
        for v in trail:
            counts[(v.kb_hash, v.node_id)] = counts.get((v.kb_hash, v.node_id), 0) + 1
        out: list[dict] = []
        seen = {(current.kb_hash, current.node_id)}
        for v in reversed(trail):
            key = (v.kb_hash, v.node_id)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kb_hash": v.kb_hash, "node_id": v.node_id, "last_visited": v.at, "visits": counts[key]})
            if len(out) >= limit:
                break
        return out
