from pathlib import Path

import pytest

from kb_topology.aliases import AliasRules

RULES = AliasRules.from_dict(
    {
        "rules": [
            {"alias": "backs", "source": "Guide", "target": "Procedure"},
            {"alias": "uses", "source": "Procedure", "target": "Table"},
            {"alias": "uses", "source": "Procedure", "target": "View"},
            {"alias": "lands", "source": "Stream", "target": "Table"},
        ]
    }
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A small OKF bundle that exercises each adapter rule."""
    root = tmp_path / "prod"
    _write(root, "VERSION", "0.2.2+20261110\n")
    _write(root, "index.md", "# Index\n[r1](/docs/guides/r1.md)\n")
    _write(root, ".cache/ignored.md", "---\ntype: Table\ntitle: ignored\n---\n")

    _write(
        root,
        "data/tables/T1.md",
        "---\ntype: Table\ntitle: alpha.S.T1\ntags: [area:alpha, group:s]\nstatus: stable\n"
        "sources:\n- resource: store://alpha/S/T1\n  id: source-1\n---\n# T1\n",
    )
    _write(root, "data/tables/T2.md", "---\ntype: View\ntitle: alpha.S.T2\n---\n# T2\n")
    _write(
        root,
        "docs/guides/r1.md",
        "---\ntype: Guide\ntitle: r1\nstatus: stable\ngenerated:\n  by: process:generate_guides\n"
        "references:\n- bundle: docs\n  id: /docs/procedures/c1.md\n  title: c1\n"
        "- bundle: data\n  id: /data/tables/MISSING.md\n  title: missing table\n"
        "- not_an_id: true\n---\n"
        "# r1\n"
        "[computation](/docs/procedures/c1.md)\n"
        "[same, relative with anchor](../procedures/c1.md#section)\n"
        "[gone](/docs/procedures/gone.md)\n"
        "[index](/index.md)\n"
        '[ext](https://example.com/x.md) <a href="/data/tables/T2.md">T2</a>\n'
        "[self](/docs/guides/r1.md)\n"
        "[not md](/data/tables/T1.csv)\n",
    )
    _write(
        root,
        "docs/procedures/c1.md",
        "---\ntype: Procedure\ntitle: c1\n---\n"
        "Uses [T1](/data/tables/T1.md) and [T2](../../data/tables/T2.md).\n",
    )
    _write(root, "feeds/streams/k1.md", "---\ntype: Stream\ntitle: k1\n---\nLands in [T1](/data/tables/T1.md)\n")
    _write(root, "misc/AR1.md", "---\ntype: 'Policy.Code:20240009'\ntitle: AR1\n---\n")
    _write(root, "misc/notype.md", "# no frontmatter at all\n")
    _write(root, "bad.md", "---\ntype: [unclosed\n---\nbody\n")
    return root
