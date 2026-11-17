# kb-topology
A Python package that navigates, searches and validates an OKF knowledge base using the topology of its links. It reads an OKF bundle (for example `.okf/prod`) without modifying it, and is used as a library, from the command line, or as an HTTP service over the same functions. There is no user interface.

What it does with a bundle:
- builds a link graph from frontmatter `references` and markdown links, and checks every link,
- proposes fixes for broken or stale references (it never edits the KB),
- answers neighborhood, shortest-path, tag, keyword and semantic queries, with graph-distance highlighting,
- keeps session trails and review decisions in its own state directory.

See [TOPOLOGY_METHODS.md](TOPOLOGY_METHODS.md) for the mathematical basis, [KB_TOPOLOGY_REQUIREMENTS.md](docs/design/KB_TOPOLOGY_REQUIREMENTS.md) for requirements and implementation status, and [KB_TOPOLOGY_DESIGN.md](docs/design/KB_TOPOLOGY_DESIGN.md) for the design.

## Install

```bash
pip install kb-topology              # core: link graph, validation, navigation, keyword search, review store (needs only PyYAML)
pip install 'kb-topology[server]'    # adds the HTTP service (FastAPI, uvicorn)
pip install 'kb-topology[semantic]'  # adds semantic search (numpy, safetensors, tokenizers)
pip install 'kb-topology[all]'
```

Python 3.13 or newer. This installs the `kb-topology` command. The semantic extra does not include the embedding model; point `KB_TOPOLOGY_MODEL_DIR` at a directory with `model.safetensors` and `tokenizer.json` (see Configuration). Releases are published to PyPI by `docs/RELEASING.md`.

## Quick start

From a source checkout:

```bash
uv sync
uv run python -m kb_topology index  --bundle .okf/prod                      # summary
uv run python -m kb_topology report --bundle .okf/prod --out artifacts      # fixes, review list, retirement report
uv run python -m kb_topology embed  --bundle .okf/prod                      # cache embeddings (about 5 minutes the first time)
uv run python -m kb_topology serve  --bundle .okf/prod --port 8100          # HTTP API, docs at /docs
uv run python -m kb_topology calibrate --apply                              # validation weights from review decisions
uv run pytest
```

## Using it as a library

```python
import kb_topology as kt

snap = kt.load_snapshot(".okf/prod")
analysis = kt.analyze(snap)                    # validations, fix proposals, retirement report
nav = kt.Navigator(analysis)

nav.neighbors("prod:/docs/guides/audit.md", direction="out", depth=1)
total, hits = kt.SearchIndex(snap).search("invoice audit report", limit=20)
fixes = [f for f in analysis.fixes if f.problem == "broken-target"]   # f.proposed is a references[] entry
```

Node ids are `<env>:<path>`, for example `prod:/docs/guides/audit.md`. The names in `kt.__all__` are the public API. `kt.create_app`, `kt.MiniLMProvider`, `kt.EmbeddingStore` and `kt.SemanticService` are imported on first use and raise an `ImportError` that names the extra to install when it is missing.

## Configuration

- The embedding model is read from the local disk: `--model-dir` or `KB_TOPOLOGY_MODEL_DIR`, default `~/.semantic_search/models/all-MiniLM-L6-v2`. No KB text leaves the machine.
- Tool state (embeddings, review decisions, session trails, `weights.json`) lives in `--state-dir` or `KB_TOPOLOGY_STATE_DIR`, default `./.kb-topology`.

## Relation aliases

Edges get a `relation_type` such as `Guide -> Procedure` from the types of their endpoints. To also give some pairs a short label, supply rules in a JSON file (`--aliases FILE`, `KB_TOPOLOGY_ALIASES`, or `aliases.json` in the state directory, or `load_snapshot(..., alias_rules=AliasRules.from_file(path))`):

```json
{"rules": [
  {"alias": "backs", "source": "Guide", "target": "Procedure"},
  {"alias": "uses",  "source": "Procedure", "target_prefix": "Data"}
]}
```

A rule needs `alias` and at least one of `source`, `target`, `source_prefix`, `target_prefix`. The first matching rule wins. Without rules, `relation_alias` is null.

## Prototype

The earlier prototype (`kb_topology/graph.py`, `main.py`) works on a synthetic KB and is separate from the above. 
## Using uv (default workflow)

```bash
uv sync
uv run main.py
```

Default run prints:
- topology cross-link suggestions,
- graph diagnostics,
- reference repair suggestions for partially written references (for example `dat-paymnts-1` -> `dat-payments-1`).

## Persisted KB repair pipeline

```bash
# Generate a broken KB, repair it, and persist outputs
uv run main.py --pipeline --output-dir artifacts
```

This writes:
- `artifacts/kb_original.json` (original KB with missing links),
- `artifacts/kb_fixed.json` (copied KB with repaired relationships),
- `artifacts/repair_suggestions.json` (all discovered repair candidates),
- `artifacts/kb_original.mmd` and `artifacts/kb_fixed.mmd` (Mermaid graphs).

Run repair on your own concept documents:

```bash
uv run main.py --pipeline --input-kb path/to/kb.json --output-dir artifacts
```

Input JSON shape:
- `concepts`: list of objects with `concept_id`, `title`, `bundle`, `tags`, `text`
- `relations`: list of objects with `source_id`, `target_id`, optional `relation_type`, `provenance`, `confidence`, `status`, and `metadata`

### Custom link categories and topology annotations

The project now supports typed edges, not just plain undirected links. Each relationship can carry its own category and provenance, so topology-generated repairs can be clearly labeled instead of looking like ordinary manual references.

Example relation payload:

```json
{
  "source_id": "ser-orders-2",
  "target_id": "dat-payments-1",
  "relation_type": "repaired-reference",
  "provenance": "topology-repair",
  "confidence": 0.91,
  "status": "accepted",
  "metadata": {
    "mention": "dat-paymnts-1",
    "reason": "topology-aware partial reference repair",
    "ambiguous": false
  }
}
```

This is the same pattern you would use for real OKF custom link categories: the relation type is explicit, and the metadata explains how and why the link was created.

## Export graph as Mermaid

```bash
# pure Mermaid script (.mmd)
uv run main.py --format mermaid --output graph.mmd

# Markdown block with ```mermaid fence
uv run main.py --format markdown --output graph.md
```

## Starting from scratch

```bash
uv python install 3.14
uv sync
uv run main.py
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
