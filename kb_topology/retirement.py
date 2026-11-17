"""Retirement candidates (design doc, section 9.3). Advice only; the tool deletes nothing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Snapshot


@dataclass(slots=True)
class RetirementItem:
    node_id: str
    path: str
    title: str
    type: str
    status: str | None
    category: str  # retirement-candidate | isolated-review
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "category": self.category,
            "signals": self.signals,
        }


def retirement_report(snap: Snapshot) -> list[RetirementItem]:
    """Nodes with no resolved link in either direction. They are candidates only with a retirement signal."""
    items: list[RetirementItem] = []
    for nid, n in snap.nodes.items():
        if snap.reverse.get(nid) or any(snap.edges[i].resolved for i in snap.forward.get(nid, ())):
            continue
        signals: list[str] = []
        if (n.status or "").lower() == "deprecated":
            signals.append("status: deprecated")
        if n.metadata.get("row_count") == 0:
            signals.append("row_count: 0")
        items.append(
            RetirementItem(
                nid, n.path, n.title, n.type, n.status,
                "retirement-candidate" if signals else "isolated-review", signals,
            )
        )
    items.sort(key=lambda i: (i.category, i.path))
    return items
