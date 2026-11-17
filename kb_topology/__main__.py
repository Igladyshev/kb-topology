"""CLI: ``python -m kb_topology index|report|embed|serve --bundle .okf/prod [--env prod]``."""

from __future__ import annotations

import json
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

from .adapter import load_snapshot
from .aliases import AliasRules
from .paths import resolve_aliases_path, resolve_model_dir, resolve_state_dir
from .state import MIN_SAMPLES, ReviewStore, StateDB, calibration
from .validation import load_weights


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_report(snapshot, out: Path) -> None:
    """Write the findings for the KB owners. Nothing is written into the bundle."""
    from .analysis import analyze

    a = analyze(snapshot)
    out.mkdir(parents=True, exist_ok=True)
    fixes = [f.to_dict() for f in a.fixes if f.problem != "body-only"]
    promote = [f.to_dict() for f in a.fixes if f.problem == "body-only"]
    review = sorted((v for v in a.validations if v.validation_status != "validated"), key=lambda v: -v.risk_score)
    _dump(out / "summary.json", a.summary())
    _dump(out / "fixes.json", fixes)
    _dump(out / "promote_to_frontmatter.json", promote)
    _dump(out / "needs_review.json", [v.to_dict() for v in review])
    _dump(out / "retirement.json", [i.to_dict() for i in a.retirement])
    print(json.dumps(a.summary(), indent=2))
    print(f"Wrote 5 files to {out}/ (fixes: {len(fixes)}, promote: {len(promote)}, needs review: {len(review)})")


def build_embeddings(snapshot, model_dir: Path, state_dir: Path) -> None:
    from .embeddings import EmbeddingStore, MiniLMProvider
    from .semantic import embedding_text

    provider = MiniLMProvider(model_dir)
    store = EmbeddingStore(state_dir, provider.model_id)
    texts = [embedding_text(n, snapshot.root) for n in snapshot.nodes.values()]
    started = time.time()

    def progress(done: int, total: int) -> None:
        print(f"\rembedding {done}/{total} ({time.time() - started:.0f}s)", end="", file=sys.stderr, flush=True)

    store.matrix(provider, texts, progress)
    print(f"\nVectors for {len(texts)} nodes cached in {store.path}", file=sys.stderr)


def run_calibration(state_dir: Path, env: str | None, apply: bool, min_samples: int) -> dict:
    """Suggest signal weights from review decisions. With ``apply``, write ``weights.json`` (read at server start)."""
    db = StateDB(state_dir / "state.db")
    weights = load_weights(state_dir)
    result = calibration(ReviewStore(db).latest(f"{env}:" if env else None), weights, min_samples)
    if apply:
        new = {name: s["suggested_weight"] for name, s in result["signals"].items()}
        (state_dir / "weights.json").write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
        result["applied_to"] = str(state_dir / "weights.json")
    db.close()
    return result


def main() -> None:
    parser = ArgumentParser(prog="kb_topology", description="Topology index over an OKF bundle (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "index": "Build the index and print a summary",
        "report": "Write validation, fix proposals and retirement report as JSON files",
        "embed": "Compute and cache node embeddings with the local model",
        "serve": "Build the index and serve the API",
    }
    for name, help_text in commands.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--bundle", type=Path, required=True, help="OKF bundle directory, e.g. .okf/prod")
        p.add_argument("--env", help="Environment name. Defaults to the bundle directory name.")
    state_help = "Tool-owned state directory (default: $KB_TOPOLOGY_STATE_DIR or ./.kb-topology)"
    for name in commands:
        p = sub.choices[name]
        p.add_argument("--aliases", type=Path, help="JSON file of relation alias rules (default: $KB_TOPOLOGY_ALIASES or <state-dir>/aliases.json)")
        p.add_argument("--state-dir", type=Path, help=state_help)
    for name in ("embed", "serve"):
        sub.choices[name].add_argument("--model-dir", type=Path, help="Local embedding model directory (default: $KB_TOPOLOGY_MODEL_DIR or ~/.semantic_search/models/all-MiniLM-L6-v2)")
    cal = sub.add_parser("calibrate", help="Suggest validation weights from review decisions (writes weights.json with --apply)")
    cal.add_argument("--state-dir", type=Path, help="Tool-owned state directory (default: $KB_TOPOLOGY_STATE_DIR or ./.kb-topology)")
    cal.add_argument("--env", help="Only use decisions for this environment")
    cal.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    cal.add_argument("--apply", action="store_true", help="Write the suggested weights to <state-dir>/weights.json")
    sub.choices["report"].add_argument("--out", type=Path, default=Path("artifacts"), help="Output directory")
    sub.choices["index"].add_argument("--diagnostics", action="store_true", help="Print full diagnostics as JSON")
    serve = sub.choices["serve"]
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8100)
    serve.add_argument("--no-semantic", action="store_true", help="Do not load the embedding model")
    args = parser.parse_args()

    if args.command == "calibrate":
        print(json.dumps(run_calibration(resolve_state_dir(args.state_dir), args.env, args.apply, args.min_samples), indent=2))
        return

    alias_path = resolve_aliases_path(args.aliases, args.state_dir)
    alias_rules = AliasRules.from_file(alias_path) if alias_path else None
    snapshot = load_snapshot(args.bundle, env=args.env, alias_rules=alias_rules)
    if args.command == "index":
        print(json.dumps(snapshot.summary(), indent=2))
        if args.diagnostics:
            print(json.dumps(snapshot.diagnostics.to_dict(), indent=2))
        return
    if args.command == "report":
        write_report(snapshot, args.out)
        return
    if args.command == "embed":
        try:
            build_embeddings(snapshot, resolve_model_dir(args.model_dir), resolve_state_dir(args.state_dir))
        except ImportError as exc:
            sys.exit(f"The embed command needs the semantic extra: pip install 'kb-topology[semantic]' ({exc})")
        except FileNotFoundError as exc:
            sys.exit(str(exc))
        return

    try:
        import uvicorn

        from .api import create_app
    except ImportError as exc:
        sys.exit(f"The serve command needs the server extra: pip install 'kb-topology[server]' ({exc})")

    provider = None
    if not args.no_semantic:
        try:
            from .embeddings import MiniLMProvider

            provider = MiniLMProvider(resolve_model_dir(args.model_dir))
        except (FileNotFoundError, ImportError) as exc:
            print(f"Semantic search disabled: {exc}. Install 'kb-topology[semantic]' and set --model-dir.", file=sys.stderr)
    state_dir = resolve_state_dir(args.state_dir)
    print(f"Serving {snapshot.env} snapshot {snapshot.kb_hash} ({len(snapshot.nodes)} nodes, {len(snapshot.edges)} edges)")
    uvicorn.run(create_app([snapshot], provider=provider, state_dir=state_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
