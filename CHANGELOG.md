# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-21

### Added

- `python -m kb_topology eval` command for measuring keyword and semantic search quality with a versioned query set.
- Business-term extraction for SQL literals and report descriptions in semantic embedding text.
- Optional `score_factor` support in keyword and semantic search, plus `status_factor` for status-based ranking.
- Snapshot family detection and `collapse=True` support for deployment-specific copies in keyword and semantic search.
- `Navigator.related()` and grouped related concepts for expanded semantic hits.
- Pre-parsed document support in `load_snapshot()` to avoid re-reading and re-parsing bundle files.
- Provider support for query and passage prefixes used by compatible embedding models.

### Changed

- Semantic embedding text now prioritizes identity, description, business terms, tags, columns, sections, and path words.
- Embedding model identities include the token cap and prefix configuration so model changes rebuild vectors safely.
- HTTP API and package metadata are prepared for the 0.2.0 release.

### Compatibility

- New search parameters default to existing behavior; callers are not required to opt in.
- Changed embedding text requires a one-time semantic index rebuild.

## [0.1.0] - 2026-11-17

### Added

- OKF bundle snapshot adapter and canonical topology model.
- Link graph construction, validation, and fix proposals.
- Navigation APIs for neighborhoods, shortest paths, tags, components, and articulation points.
- BM25 keyword search and topology-aware semantic search.
- Local embedding provider and persistent embedding cache.
- Review decisions, session trails, validation weights, and calibration.
- HTTP API and command-line commands for indexing, reporting, embedding, calibration, and serving.
