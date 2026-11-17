"""Locations of the embedding model and the tool-owned state directory."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL_DIR = Path.home() / ".semantic_search" / "models" / "all-MiniLM-L6-v2"
MODEL_DIR_ENV = "KB_TOPOLOGY_MODEL_DIR"
STATE_DIR_ENV = "KB_TOPOLOGY_STATE_DIR"
ALIASES_ENV = "KB_TOPOLOGY_ALIASES"


def resolve_model_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(MODEL_DIR_ENV) or DEFAULT_MODEL_DIR).expanduser()


def resolve_state_dir(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get(STATE_DIR_ENV) or ".kb-topology").expanduser()


def resolve_aliases_path(explicit: str | Path | None = None, state_dir: str | Path | None = None) -> Path | None:
    """Alias rules file: the explicit path, else $KB_TOPOLOGY_ALIASES, else <state-dir>/aliases.json if it exists."""
    chosen = explicit or os.environ.get(ALIASES_ENV)
    if chosen:
        return Path(chosen).expanduser()
    default = resolve_state_dir(state_dir) / "aliases.json"
    return default if default.is_file() else None
