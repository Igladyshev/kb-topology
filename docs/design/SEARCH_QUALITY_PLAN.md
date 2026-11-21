# Search quality: plan for kb-topology 0.2

Status: **implementing** (2026-11-21). Input: feedback from an agent building a
client for a Knowledge Base, checked against a representative non-production
bundle. The KB server already applies some of it on top of
kb-topology 0.1.0; this plan moves the parts that belong in the engine into
kb-topology, and adds what only the engine can do.

## 1. Problems, measured

| # | Problem | Evidence (representative bundle) |
|---|---|---|
| P1 | The embedded text misses a concept's business vocabulary. | `embedding_text()` embeds title, type, tags, description, path words, column names and section headings, cut at 128 tokens. For computations and reports the words users search with are in the SQL; search-relevant string literals and report descriptions may be absent from the embedding. |
| P2 | No way to measure a change. | There is no fixed query set or score. Any change to the text, the token cap or the model is judged by eye. |
| P3 | Ranking adjustments happen outside the engine. | The KB server down-ranks deprecated concepts by fetching 5× the requested results and re-sorting. The engine could do this before it cuts to `top_k`. |
| P4 | Per-deployment copies are not known to the engine. | Multiple deployment-specific copies can crowd a result set for one logical concept. The KB server collapses them by path; the front matter has `fully_qualified_name` and `deployment_code` for an exact rule. |
| P5 | Link expansion results do not say where they came from in a usable way. | `SemanticHit.evidence["matched_from"]` exists, but callers that want "the related concepts of each match" must rebuild the mapping and the direction themselves. |
| P6 | The bundle is parsed twice. | `load_snapshot()` re-reads and re-parses all files the KB server has already read, adding avoidable load time. |

## 2. Out of scope (KB generators)

- Marking test and backup copies (`…_TEST`, `…_BKP`, `…_BACKUP`) with a tag or status, so both engines can down-rank them like
  deprecated concepts (T5 then applies to them too).
- A glossary concept generated from deployment-specific variants and their
  computation SQL.
- Fixing a deprecation rule for objects built on shared tables and still in use.

## 3. Design

### 3.1 Evaluation set (P2)

A versioned file of queries with the concepts a good answer contains, and a
scorer:

```yaml
# kb_topology/eval/example_queries.yaml
- query: "excluded users"
  relevant: ["/reports/exclusion_report.md", "..."]
- query: "customer identity"
  relevant: ["/databases/prod/core/tables/IdentityRecord.md", "..."]
```

`python -m kb_topology eval --root .okf/prod --queries example_queries.yaml
--engine keyword|semantic` prints recall@10, MRR and the per-query rank of the
first relevant hit, and writes a JSON report to compare runs. Start with the 20
queries in the KB server's `docs/kb-topology-integration.md`.

### 3.2 Business terms in the embedded text (P1)

`body_hints()` also returns **business terms**: distinct string literals from
the body's SQL that read as words, from `THEN '…'` / `ELSE '…'`,
`IN ('…', …)`, `= '…'` and `AS "…"` aliases, plus report `DESCRIPTION` values.
Filter out codes and dates (no letters, or no spaces and all caps of ≤ 4
characters), keep the first 40 by first appearance.

`embedding_text()` order, most important first because of truncation:

```
title. type. description. terms: <business terms>. tags. columns: <…>. sections: <…>. <path words>
```

Embeddings are keyed by a hash of the text, so changed concepts re-embed by
themselves; the text change needs no migration.

### 3.3 Token cap (P1)

`MiniLMProvider(max_tokens=…)` is already a parameter. Measure 128, 256 and
384 with 3.1 and the build time on a representative bundle; make the best one the
default. The model id already carries the cap (`…-t128`), so a change rebuilds
every vector.

### 3.4 One vector per section (P1, optional)

For concepts whose body exceeds the cap, embed the identity text plus each
`##` section separately and score a concept by its best section (max-sim). The
index grows about 3–4×, so this is behind `SemanticService(multi_vector=True)`
and only worth it with a prebuilt index artifact. Decide with 3.1.

### 3.5 Model choice (P1, after 3.1)

| Model | Dimensions | Layers | Size | Needs |
|---|---|---|---|---|
| all-MiniLM-L6-v2 (today) | 384 | 6 | 90 MB | — |
| bge-small-en-v1.5 | 384 | 12 | 133 MB | query instruction prefix |
| e5-small-v2 | 384 | 12 | 133 MB | `query: ` / `passage: ` prefixes |

The 12-layer models take about twice as long to embed. all-MiniLM-L6-v2 is
Apache-2.0; bge-small and e5-small are MIT; all three are on the approved
licence list. A provider gets `query_prefix` / `passage_prefix`; the model id
changes, so all vectors rebuild. Switch only if 3.1 shows a clear gain.

### 3.6 Score adjustment inside the engine (P3)

`SearchIndex.search(…, score_factor=None)` and
`SemanticService.search(…, score_factor=None)` take a callable
`score_factor(node) -> float` (default 1.0) applied before sorting and the
`limit` / `top_k` cut. A ready-made `status_factor({"deprecated": 0.5})`
helper covers the common case. Callers then get the right top results without
over-fetching.

### 3.7 Families of copies (P4)

`Snapshot.family_of(node_id) -> str` and `Snapshot.families() -> dict[str,
list[str]]`. The key is the `fully_qualified_name` with its `deployment_code`
segment replaced by `*` when both are in the front matter, else the node id.
Both search APIs get `collapse=True|False`; collapsed hits carry
`copies: list[node_id]` and the best copy is returned.

### 3.8 Related concepts per match (P5)

`SemanticService.search(…, group_expanded=True)` returns direct hits each with
`related: [{node_id, distance, direction, score}]` instead of a flat list
after them. `Navigator.related(node_id, depth, limit)` gives the same for
keyword hits, with `direction` = `downstream` / `upstream` for distance 1.

### 3.9 Pre-parsed documents (P6)

`load_snapshot(root, documents=…)` accepts an iterable of
`(path, frontmatter_dict, body)` the caller has already parsed, and skips its
own read and YAML parse. Results must be identical to reading from disk.

## 4. Tasks

| ID | Task | Acceptance |
|---|---|---|
| T1 | Evaluation set and `eval` command (3.1) | Runs on a representative bundle; report has recall@10, MRR, per-query ranks; 20+ queries with relevant concepts |
| T2 | Baseline | T1 report for keyword and semantic on 0.1.0, kept in `docs/eval/` |
| T3 | Business terms (3.2) | `body_hints` returns terms for a computation page; unit tests for each SQL form; T1 semantic MRR not lower than T2, and higher on representative business-vocabulary queries |
| T4 | Token cap (3.3) | T1 results and build times for 128/256/384 in `docs/eval/`; default set to the best |
| T5 | `score_factor` (3.6) | Both engines; deprecated example ranks below the live one before the cut; no behaviour change when unset |
| T6 | Families and `collapse` (3.7) | Deployment-specific variants collapse to one hit with all copies listed; unit test with three deployments |
| T7 | Related per match (3.8) | Direct hits carry `related` with direction; flat output unchanged by default |
| T8 | Pre-parsed documents (3.9) | Snapshot equal to the disk-read one on a test bundle; documented |
| T9 | Model option (3.5), only if T3/T4 leave a gap | Provider prefixes; T1 comparison of the three models; recommendation in `docs/eval/` |
| T10 | Multi-vector (3.4), only if T3/T4 leave a gap | Behind a flag; T1 comparison and index size |
| T11 | Release 0.2.0 | Changelog; `docs/RELEASING.md` steps; the KB server moves its deprecated penalty and collapsing onto T5/T6 |

Order: T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T11; T9 and T10 only on
evidence.

## 5. Compatibility

All new parameters default to today's behaviour. T3 and T4 change the
embedded text or the model id, so a deployed index is rebuilt once (the KB
server's CI builds it as a cached artifact). Release as 0.2.0.

---
_Drafted and reviewed by project maintainers._
