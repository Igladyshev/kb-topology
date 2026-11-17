import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import kb_topology as kt


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True)


def test_every_exported_name_resolves():
    missing = [n for n in kt.__all__ if not hasattr(kt, n)]
    assert missing == []
    assert set(kt.__all__) <= set(dir(kt))


def test_version_is_a_release_string():
    assert kt.__version__ and kt.__version__[0].isdigit()


def test_typed_marker_is_packaged():
    assert (Path(kt.__file__).parent / "py.typed").is_file()


def test_core_import_needs_neither_numpy_nor_fastapi():
    r = run_python(
        """
        import sys
        import kb_topology as kt
        kt.load_snapshot, kt.analyze, kt.Navigator, kt.SearchIndex, kt.ReviewStore
        loaded = [m for m in ("numpy", "fastapi", "uvicorn", "safetensors", "tokenizers") if m in sys.modules]
        print(loaded)
        """
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_server_import_does_not_pull_in_numpy():
    r = run_python(
        """
        import sys
        import kb_topology as kt
        kt.create_app
        print("numpy" in sys.modules)
        """
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"


@pytest.mark.parametrize(
    ("name", "blocked", "extra"),
    [("create_app", "fastapi", "server"), ("MiniLMProvider", "numpy", "semantic")],
)
def test_missing_extra_gives_an_install_hint(name: str, blocked: str, extra: str):
    r = run_python(
        f"""
        import sys
        sys.modules[{blocked!r}] = None   # makes `import {blocked}` fail
        import kb_topology as kt
        try:
            getattr(kt, {name!r})
        except ImportError as exc:
            print(exc)
        """
    )
    assert r.returncode == 0, r.stderr
    assert f"kb-topology[{extra}]" in r.stdout


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError):
        kt.nope  # noqa: B018


def test_cli_runs_from_the_installed_module(tmp_path: Path):
    bundle = tmp_path / "dev"
    (bundle / "a").mkdir(parents=True)
    (bundle / "a" / "x.md").write_text("---\ntype: Concept\ntitle: X\n---\n[y](/a/y.md)\n")
    (bundle / "a" / "y.md").write_text("---\ntype: Concept\ntitle: Y\n---\n")
    r = subprocess.run([sys.executable, "-m", "kb_topology", "index", "--bundle", str(bundle)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert '"nodes": 2' in r.stdout and '"edges": 1' in r.stdout and '"env": "dev"' in r.stdout


def test_serve_without_semantic_extra_still_starts(tmp_path: Path):
    """`kb-topology serve` must degrade to no semantic search when numpy is missing, not crash."""
    bundle = tmp_path / "dev"
    (bundle / "a").mkdir(parents=True)
    (bundle / "a" / "x.md").write_text("---\ntype: Concept\ntitle: X\n---\n")
    code = f"""
    import sys
    sys.modules["numpy"] = None
    sys.modules["uvicorn"].run = lambda app, **kw: print("started", sorted(p for p in app.openapi()["paths"] if "semantic/status" in p))
    sys.argv = ["kb-topology", "serve", "--bundle", {str(bundle)!r}, "--state-dir", {str(tmp_path / "state")!r}]
    from kb_topology.__main__ import main
    main()
    """
    r = run_python("import uvicorn\n" + textwrap.dedent(code))
    assert r.returncode == 0, r.stderr
    assert "Semantic search disabled" in r.stderr and "started" in r.stdout
