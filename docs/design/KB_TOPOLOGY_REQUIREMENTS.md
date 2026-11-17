# KB Topology Requirements

## 1. Goal

Provide fast, topology-based navigation, search and link validation over a large OKF knowledge base, as a Python package. The measured baseline is one real bundle with **14,197 concepts and 82,332 links** (16,472 declared in frontmatter, 65,860 only in markdown bodies). The KB is expected to grow. The package must remain usable well beyond these numbers.

## 2. Scope

- Input: an already-built OKF bundle directory for one environment (for example `.okf/prod`), read from the local disk. How the bundle gets there is up to the caller. The package never modifies it.
- Upstream generation of the KB is out of scope.
- The package builds an index that holds node identifiers, derived metrics and references into the KB, not copies of KB documents.
- Delivery: a Python package (`kb-topology`, import name `kb_topology`) with a library API, a command-line tool (`kb-topology`), and an optional HTTP service over the same functions. Python 3.13 or newer. Open source under the Apache License 2.0. The core depends only on PyYAML. Extras: `server` (FastAPI, uvicorn) and `semantic` (numpy, safetensors, tokenizers). There is no user interface. Callers are scripts, services and agents.
- The embedding model runs from the local disk. No KB text is sent to any hosted service.

## 3. Functional requirements

1. Build a versioned topology index per bundle snapshot, identified by `kb_hash`. The hash covers the environment, the bundle `VERSION` and the content hash of every concept file.
2. Node ids are `<env>:<kb-path>` so they are unique across environments.
3. Edges come from OKF frontmatter `references[]` and from markdown links in concept bodies. Each edge records which of the two declared it, its provenance (the declaring file's `generated.by`), a derived `relation_type` (the endpoint type pair), and an optional derived `relation_alias` from alias rules the caller supplies (without rules it is null).
4. Edges to missing targets are kept and flagged, not dropped.
5. Neighborhood and shortest-path queries, with filters on edge attributes and on node attributes, and options to keep paths off hub pages.
6. Connected components and articulation points.
7. Keyword search over concept metadata, and semantic search that returns direct matches plus nodes highlighted by graph distance from them.
8. Tags are first-class facets: tag listing, tag filters on neighbors, tag neighborhoods (the members of a tag and the nodes just outside it).
9. Session trails: visited nodes in order with timestamps, the current node, and jump-back options. Trails persist across restarts.
10. Validate every existing edge and classify it as `validated`, `needs-review` or `likely-misleading`, with a risk score, a confidence, machine-readable evidence and a recommended action.
11. For flagged edges, return fix proposals that can be pasted into the KB: the current reference, ranked candidate targets, and a ready-to-use `references[]` entry. The package never applies a fix.
12. Report retirement candidates (no links and a retirement signal such as `deprecated` or zero rows). The package never deletes anything.
13. Persist human review decisions in a store outside the KB, mark them stale when either endpoint file changes, and suggest validation weights from them.
14. No hard maximum KB size.

## 4. Performance requirements

Baseline corpus: one real bundle (14.2K concepts, 82K links). Targets are for a single process on one machine.

| Operation | Target | Measured on the baseline bundle (p95 unless noted) |
|---|---|---|
| Build the index from the bundle (read, parse, links, validation, indexes) | 5 s | 4.0 s |
| Load the cached embeddings | 3 s | about 1 s |
| Neighborhood, depth 1 | 50 ms | 0.1 ms |
| Neighborhood, depth 2 | 50 ms | 2.5 ms |
| Filtered neighborhood | 80 ms | under 0.1 ms |
| Shortest path | 120 ms | 13 ms |
| Keyword search, top 20 | 150 ms | 6 ms |
| Semantic search including expansion | 150 ms | 12 to 21 ms (single queries) |
| Session write | 20 ms | 0.4 ms |
| First full embedding of all nodes | none (one-off) | about 5.5 minutes on one CPU core |

Measurements are from one run on a development laptop, not a load test. Targets must hold at the baseline and degrade gracefully at 2× (30K concepts, 160K links) and 5× scale.

## 5. Data model

See section 6 of the design document for the exact fields.

- `Node`: `id`, `env`, `path`, `title`, `type`, `type_family`, `type_qualifier`, `bundle`, `tags`, `status`, `source_refs`, `kb_ref` (path and content hash), `description` (first 300 characters), small scalar `metadata`.
- `Edge`: `source_id`, `target_id`, `declared_in`, `declared_bundle`, `declared_title`, `provenance`, `declared_status`, `resolved`, `relation_type`, `relation_alias`, `confidence`, `declared_ref_index`.
- `Snapshot`: `kb_hash`, `env`, `version`, nodes, edges, diagnostics.
- `EdgeValidation`, `FixProposal`, `RetirementItem`, `SemanticHit`, `ReviewRecord`, session `Visit`.

## 6. Caching and index strategy

1. The bundle is the persistent source. The index is rebuilt from it at start and is deterministic for a given `kb_hash`.
2. The active snapshot is held in memory.
3. Embeddings are cached on disk, keyed by a hash of the model id and the exact text, so a rebuild embeds only changed text.
4. Connected components, articulation points, degrees and tag indexes are computed once per snapshot.
5. Keyword search uses an in-memory inverted index.
6. Not implemented: a persisted adjacency snapshot, LRU caches for queries, incremental re-indexing of changed concepts, and sharding. Queries run in milliseconds at baseline, so they are deferred until scale requires them.

## 7. Interface requirements

The same functions are available as a Python library, a CLI, and an HTTP service. The HTTP endpoints are listed in section 7 of the design document. All graph endpoints support filtering by relation alias and type, declaring source, validation status and confidence, and by node bundle, type family, tag and status. Listing endpoints support paging. Validation listings are sorted by `risk_score`.

## 8. Reliability requirements

- The same bundle always produces the same `kb_hash` and the same index.
- The package never writes to the bundle.
- Tool state (embeddings, review decisions, sessions, weights) lives in one state directory outside the KB.
- Review decisions are append-only and auditable. Each records the risk and fired signals at decision time.
- A missing or invalid concept file is reported in diagnostics and does not stop the build.
- Not implemented: atomic publication of a new snapshot and fallback to the previous one. The service loads one bundle at start.

## 9. Acceptance criteria

1. The baseline bundle builds within the build target above, and queries meet the latency targets.
2. Session trails and review decisions persist and restore across restarts.
3. A decision becomes stale when either endpoint file changes, and the edge returns to the review queue.
4. Every flagged edge is classified with a status, a risk score, evidence and a recommended action.
5. Every broken link has a ranked list of candidate targets, and a proposal when one candidate is clearly best.
6. Search and tag filtering work on node metadata.
7. Relative links, fragments and query strings in body links resolve correctly, and links to missing files are reported rather than dropped.
8. Semantic search returns direct matches and highlighted neighbors with scores that follow `match_score × exp(−α × distance)`.
9. No KB text leaves the machine.

## 10. Reference validation and mislink detection

The package validates existing edges, not only suggests missing ones.

### 10.1 Detection signals (implemented)

1. Target does not exist (`broken_target`).
2. Declared bundle differs from the target's bundle (`bundle_mismatch`).
3. Declared title differs from the target's title (`title_mismatch`).
4. Target is deprecated while the source is not (`deprecated_target`).
5. Rare endpoint type pair for the source type (`type_outlier`).
6. Endpoints share no neighbors (`discontinuity`).

Not implemented: a semantic mismatch score between endpoints. The field exists and is null.

### 10.2 Output contract

Each evaluated edge produces `validation_status`, `risk_score` (0..1), `confidence` (1 − risk), `scores`, `evidence` (kind, detail, score), `recommended_action`, and `validated_at`. Recommended actions are `none`, `retarget-or-remove`, `update-bundle`, `update-title`, `review-deprecated-target`, and `review`.

### 10.3 Safety and workflow

1. The validator never changes the KB.
2. High-risk links appear first in the review queue.
3. Human decisions are persisted and feed calibration suggestions. Suggested weights are applied only when someone runs `calibrate --apply`.
4. Incremental validation of changed nodes and scheduled full audits are not implemented. A full validation takes about 1 second on the baseline bundle.

## 11. Implementation status

| Area | Status |
|---|---|
| OKF adapter, link graph, configurable relation aliases | Done |
| Validation, fix proposals, retirement report | Done |
| Navigation, tags, topology, keyword search | Done |
| Semantic search with graph highlighting (local model) | Done |
| Session trails, review store, calibration (SQLite) | Done |
| Installable distribution (hatchling wheel and sdist, extras, console script `kb-topology`, public API in `kb_topology.__all__`, typed, Apache-2.0) | Done |
| CI (tests on 3.13 and 3.14, build) and tag-triggered publish workflow | Written, not yet run on GitHub |
| Publish to PyPI | Not done. Needs the one-time PyPI setup in `docs/RELEASING.md` and a pushed tag |
| Incremental refresh, query caches, sharding | Not done |
| Atomic snapshot publish and fallback | Not done |
| Authentication and access control on the HTTP service | Not done |
