"""Relation labels derived from the types of an edge's two endpoints.

``relation_type`` is set on every resolved edge as "<source type family> -> <target type family>".
``relation_alias`` is an optional short label for some type pairs. The KB does not declare either
one, so both are computed. Aliases come from rules the caller supplies. With no rules, every
``relation_alias`` is None.

A rules file is JSON::

    {"rules": [
      {"alias": "backs", "source": "Report", "target": "Computation"},
      {"alias": "uses",  "source": "Computation", "target_prefix": "Table"}
    ]}

Each rule needs ``alias`` and at least one of ``source``, ``target``, ``source_prefix`` and
``target_prefix``. All the conditions a rule gives must hold, and the first matching rule wins.
Types are compared by type family (the frontmatter ``type`` up to the first ``:``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONDITIONS = ("source", "target", "source_prefix", "target_prefix")


def relation_type(source_family: str, target_family: str) -> str:
    return f"{source_family} -> {target_family}"


@dataclass(frozen=True, slots=True)
class AliasRule:
    alias: str
    source: str | None = None
    target: str | None = None
    source_prefix: str | None = None
    target_prefix: str | None = None

    def matches(self, source_family: str, target_family: str) -> bool:
        return (
            (self.source is None or source_family == self.source)
            and (self.target is None or target_family == self.target)
            and (self.source_prefix is None or source_family.startswith(self.source_prefix))
            and (self.target_prefix is None or target_family.startswith(self.target_prefix))
        )


class AliasRules:
    """An ordered list of rules. The first rule that matches an endpoint pair gives the alias."""

    def __init__(self, rules: list[AliasRule] | None = None) -> None:
        self.rules = list(rules or [])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AliasRules:
        raw = data.get("rules")
        if not isinstance(raw, list):
            raise ValueError("alias rules must be an object with a 'rules' list")
        rules = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"rule {i} is not an object")
            unknown = set(item) - {"alias", *_CONDITIONS}
            if unknown:
                raise ValueError(f"rule {i} has unknown keys: {sorted(unknown)}")
            alias = item.get("alias")
            if not isinstance(alias, str) or not alias:
                raise ValueError(f"rule {i} needs a non-empty 'alias'")
            conditions = {k: item[k] for k in _CONDITIONS if k in item}
            if not conditions or not all(isinstance(v, str) and v for v in conditions.values()):
                raise ValueError(f"rule {i} needs at least one non-empty string condition from {list(_CONDITIONS)}")
            rules.append(AliasRule(alias, **conditions))
        return cls(rules)

    @classmethod
    def from_file(cls, path: str | Path) -> AliasRules:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def alias(self, source_family: str, target_family: str) -> str | None:
        for rule in self.rules:
            if rule.matches(source_family, target_family):
                return rule.alias
        return None

    def __len__(self) -> int:
        return len(self.rules)
