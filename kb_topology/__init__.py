"""kb_topology: navigate, search and validate an OKF knowledge base using the topology of its links.

Typical use::

    import kb_topology as kt

    snap = kt.load_snapshot(".okf/prod")      # reads the bundle, never writes to it
    analysis = kt.analyze(snap)               # validations, fix proposals, retirement report
    nav = kt.Navigator(analysis)
    nav.neighbors("prod:/area/group/item.md", direction="out")

The core needs only PyYAML. Names that need an optional extra (``kb-topology[semantic]`` for
the embedding model, ``kb-topology[server]`` for the HTTP service) are imported on first use.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .adapter import load_snapshot
from .aliases import AliasRule, AliasRules
from .analysis import Analysis, analyze
from .fixes import FixProposal
from .model import Diagnostics, Edge, KbRef, Node, Snapshot
from .navigation import EdgeCriteria, Navigator, NodeCriteria
from .retirement import RetirementItem
from .search import SearchIndex
from .sessions import SessionStore
from .state import ReviewRecord, ReviewStore, StateDB
from .validation import EdgeValidation

# The earlier prototype (synthetic KB, reference repair). Kept importable from the package root.
from .graph import (
    Concept,
    CrossLinkSuggestion,
    ReferenceRepairSuggestion,
    RelationRecord,
    TopologyDiagnostics,
    TopologyKnowledgeGraph,
)

try:
    __version__ = version("kb-topology")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"

# name -> (module, extra that provides its dependencies)
_OPTIONAL: dict[str, tuple[str, str]] = {
    "create_app": ("kb_topology.api", "server"),
    "MiniLMProvider": ("kb_topology.embeddings", "semantic"),
    "EmbeddingStore": ("kb_topology.embeddings", "semantic"),
    "SemanticService": ("kb_topology.semantic", "semantic"),
}

if TYPE_CHECKING:
    from .api import create_app
    from .embeddings import EmbeddingStore, MiniLMProvider
    from .semantic import SemanticService


def __getattr__(name: str) -> Any:
    if name in _OPTIONAL:
        module, extra = _OPTIONAL[name]
        try:
            value = getattr(import_module(module), name)
        except ImportError as exc:
            raise ImportError(f"kb_topology.{name} needs the '{extra}' extra: pip install 'kb-topology[{extra}]'") from exc
        globals()[name] = value
        return value
    raise AttributeError(f"module 'kb_topology' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_OPTIONAL))


__all__ = [
    "__version__",
    # build and analyze
    "load_snapshot",
    "AliasRules",
    "AliasRule",
    "analyze",
    "Analysis",
    "Snapshot",
    "Node",
    "Edge",
    "KbRef",
    "Diagnostics",
    "EdgeValidation",
    "FixProposal",
    "RetirementItem",
    # query
    "Navigator",
    "EdgeCriteria",
    "NodeCriteria",
    "SearchIndex",
    # state
    "StateDB",
    "ReviewStore",
    "ReviewRecord",
    "SessionStore",
    # optional extras, imported on first use
    "create_app",
    "MiniLMProvider",
    "EmbeddingStore",
    "SemanticService",
    # prototype
    "Concept",
    "CrossLinkSuggestion",
    "ReferenceRepairSuggestion",
    "RelationRecord",
    "TopologyDiagnostics",
    "TopologyKnowledgeGraph",
]
