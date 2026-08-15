"""`GraphCache`: the in-memory tier of CLAUDE.md's two-tier caching
architecture.

Holds the process's current `networkx.DiGraph` (built by
`backend/graph/builder.py`), rebuilt **once per refresh, never once per
request**. Routers must depend on `get_graph_cache().get_graph()` only --
never call `build_graph()` directly -- so there is exactly one place that
decides when a rebuild happens.

Lifecycle: `rebuild()` is called exactly twice in normal operation --
once at backend startup (`backend/main.py`, pre-warming from whatever's
already on disk, even before any refresh has run this process) and once by
the `/refresh` router immediately after `run_refresh()` reports success.
Nothing else should call it.

Atomic swap, not in-place mutation
--------------------------------------
`rebuild()` builds the *new* graph as a completely separate object (via
`build_graph`, which is already pure -- see its own docstring), then
reassigns `self._graph` to it in one Python attribute-set, which is
atomic under the GIL. A request that already called `get_graph()` and is
mid-traversal keeps its own reference to the old, still-fully-intact
graph object; a request that calls `get_graph()` a moment later gets the
new one. Neither ever observes a half-built graph, and no lock is needed
on the read path -- exactly CLAUDE.md's requirement. `rebuild()` itself
*is* guarded by a lock (`_rebuild_lock`) purely so two concurrent
`rebuild()` calls can't interleave their swaps of `_graph` /
`_data_version` / `_warnings` into a mismatched combination; it is not
what prevents overlapping *refreshes* end-to-end (fetching + writing +
rebuilding) -- that's the `/refresh` router's own in-process lock around
the whole `run_refresh()` + `rebuild()` sequence, a later task.

Data version
---------------
`get_data_version()` mirrors `backend.models.db.get_current_data_version`
at the moment of the last `rebuild()` -- the `RefreshRun.id` of the most
recent successful refresh, or `None` if none has ever succeeded. This is
the version component of the `/route` LRU cache key described in
CLAUDE.md (a later task): it changes exactly when the graph changes, so
that cache is self-invalidating on every refresh with no manual
`cache_clear()` needed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
from sqlalchemy import Engine

from backend.config import Settings, get_settings
from backend.graph.builder import build_graph
from backend.models.db import get_current_data_version, session_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphCacheSnapshot:
    """A consistent, point-in-time view of everything `rebuild()` produced.

    Returned by `rebuild()` itself (for a caller like the `/refresh` router
    that wants the fresh counts/warnings immediately) and readable
    piecemeal afterwards via `GraphCache`'s individual getters.
    """

    graph: nx.DiGraph
    data_version: Optional[int]
    commodity_names: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class GraphCache:
    """Process-wide singleton holding the current graph. Use
    `get_graph_cache()`, not this class directly, outside of tests.
    """

    def __init__(self) -> None:
        self._rebuild_lock = threading.Lock()
        # Starts as a valid, empty graph -- never `None` -- so a router that
        # calls `get_graph()` before the startup pre-warm has run (or if it
        # ever failed) gets a well-formed "nothing known yet" graph rather
        # than needing a null check.
        self._snapshot = GraphCacheSnapshot(graph=nx.DiGraph(), data_version=None, warnings=[])

    def get_snapshot(self) -> GraphCacheSnapshot:
        """Atomic read of graph + data_version + commodity_names + warnings
        together, for a caller (e.g. a `/route` or `/terminals` router) that
        needs a mutually consistent view of all of them -- a single
        attribute read, so it can never straddle a concurrent `rebuild()`
        the way two independent `get_graph()` / `get_data_version()` calls
        could.
        """
        return self._snapshot

    def get_graph(self) -> nx.DiGraph:
        """Instant: returns whatever graph the last successful `rebuild()` produced."""
        return self._snapshot.graph

    def get_data_version(self) -> Optional[int]:
        return self._snapshot.data_version

    def get_commodity_names(self) -> dict[int, str]:
        return dict(self._snapshot.commodity_names)

    def get_warnings(self) -> list[str]:
        return list(self._snapshot.warnings)

    def rebuild(
        self, engine: Optional[Engine] = None, settings: Optional[Settings] = None
    ) -> GraphCacheSnapshot:
        """Build a fresh graph from the cache DB's current contents and
        atomically swap it in. Safe to call with no prior refresh in this
        process (startup pre-warm) -- builds from whatever's already on
        disk, which may be nothing (empty DB -> empty graph, not an error).
        """
        settings = settings or get_settings()

        with self._rebuild_lock:
            build_result = build_graph(engine=engine, settings=settings)
            with session_scope(engine) as session:
                data_version = get_current_data_version(session)

            snapshot = GraphCacheSnapshot(
                graph=build_result.graph,
                data_version=data_version,
                commodity_names=build_result.commodity_names,
                warnings=build_result.warnings,
            )
            self._snapshot = snapshot  # atomic swap -- see module docstring

        logger.info(
            "graph cache rebuilt: nodes=%d edges=%d data_version=%s warnings=%d",
            snapshot.graph.number_of_nodes(),
            snapshot.graph.number_of_edges(),
            snapshot.data_version,
            len(snapshot.warnings),
        )
        return snapshot


_graph_cache: Optional[GraphCache] = None


def get_graph_cache() -> GraphCache:
    """Return the process-wide `GraphCache` singleton, creating it on first call.

    Mirrors `backend.models.db.get_engine()`'s lazy-singleton pattern.
    """
    global _graph_cache
    if _graph_cache is None:
        _graph_cache = GraphCache()
    return _graph_cache
