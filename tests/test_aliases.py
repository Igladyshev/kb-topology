import json
from pathlib import Path

import pytest

from kb_topology.aliases import AliasRule, AliasRules, relation_type
from kb_topology.paths import resolve_aliases_path


def test_relation_type_joins_the_two_families():
    assert relation_type("Guide", "Procedure") == "Guide -> Procedure"


def test_no_rules_means_no_alias():
    assert AliasRules().alias("Guide", "Procedure") is None
    assert len(AliasRules()) == 0


def test_exact_and_prefix_conditions_must_all_hold():
    rules = AliasRules.from_dict(
        {
            "rules": [
                {"alias": "exact", "source": "Guide", "target": "Procedure"},
                {"alias": "by_target_prefix", "source": "Procedure", "target_prefix": "Data"},
                {"alias": "by_source_prefix", "source_prefix": "Feed"},
                {"alias": "by_target_only", "target": "Index"},
            ]
        }
    )
    assert rules.alias("Guide", "Procedure") == "exact"
    assert rules.alias("Guide", "Other") is None
    assert rules.alias("Procedure", "DataTable") == "by_target_prefix"
    assert rules.alias("Report", "DataTable") is None  # source condition fails
    assert rules.alias("FeedA", "Anything") == "by_source_prefix"
    assert rules.alias("Anything", "Index") == "by_target_only"


def test_first_matching_rule_wins():
    rules = AliasRules([AliasRule("first", source="A"), AliasRule("second", source="A", target="B")])
    assert rules.alias("A", "B") == "first"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"rules": "no"},
        {"rules": ["no"]},
        {"rules": [{"source": "A"}]},  # no alias
        {"rules": [{"alias": "x"}]},  # no condition
        {"rules": [{"alias": "x", "source": ""}]},
        {"rules": [{"alias": "x", "source": 3}]},
        {"rules": [{"alias": "x", "source": "A", "typo": "B"}]},
    ],
)
def test_invalid_rules_are_rejected(data):
    with pytest.raises(ValueError):
        AliasRules.from_dict(data)


def test_rules_file_and_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    state.mkdir()
    default = state / "aliases.json"
    default.write_text(json.dumps({"rules": [{"alias": "a", "source": "X", "target": "Y"}]}))
    assert AliasRules.from_file(default).alias("X", "Y") == "a"

    monkeypatch.delenv("KB_TOPOLOGY_ALIASES", raising=False)
    assert resolve_aliases_path(None, state) == default
    assert resolve_aliases_path(None, tmp_path / "empty") is None
    monkeypatch.setenv("KB_TOPOLOGY_ALIASES", str(tmp_path / "env.json"))
    assert resolve_aliases_path(None, state) == tmp_path / "env.json"
    assert resolve_aliases_path(tmp_path / "flag.json", state) == tmp_path / "flag.json"
