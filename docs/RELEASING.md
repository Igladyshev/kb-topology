# Releasing kb-topology

## Status

- **License:** Apache-2.0 (`LICENSE`, `NOTICE`, `license` in `pyproject.toml`). The copyright line is "Igor Gladyshev". If the code belongs to an employer, the copyright holder in `NOTICE` should be changed to match before the first release.
- **Public content:** the package is open source and is published to public PyPI. The repository contains no knowledge-base content and no organization-specific type names. Type-to-alias rules are supplied by the user at run time. Tests build small sample bundles on the fly.
- **Name:** `kb-topology` returned 404 on pypi.org when checked, so it appeared unclaimed. It is claimed at the first upload. Check again just before.
- **Python:** 3.13 or newer.

## One-time PyPI setup (trusted publishing)

On pypi.org, Account settings, Publishing, add a pending publisher: project `kb-topology`, owner `Igladyshev`, repository `kb-topology`, workflow `publish.yml`, environment `pypi`. In GitHub, create an environment named `pypi` (optionally with required reviewers). No API token is stored.

## Cutting a release

1. Set `version` in `pyproject.toml` (PyPI never allows re-uploading a version). Commit to `main` after CI passes.
2. Check locally:
   ```bash
   uv sync && uv run pytest -q
   rm -rf dist && uv build && uv run twine check dist/*
   ```
3. Optional dry run against TestPyPI: `uv run twine upload --repository testpypi dist/*`, then `pip install -i https://test.pypi.org/simple/ kb-topology`.
4. Tag and push (this uploads to the public index, and a version can never be re-uploaded): `git tag v0.1.0 && git push origin v0.1.0`. The `publish.yml` workflow checks that the tag equals the version, runs the tests, builds, and uploads.

## What is verified before a release

The built wheel is installed into clean environments and checked: the core (`kb-topology index` on a bundle), the `server` extra (HTTP service answering, semantic search reported as disabled), and the `all` extra (embedding output equal to the reference values). `twine check` must pass for both files. See the commands in the build step above.
