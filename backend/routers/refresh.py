"""`POST /refresh` and `GET /refresh-status`.

`POST /refresh` runs the ingestion pipeline (`backend/ingest/refresh.py`)
synchronously against the live external APIs, and -- only on success --
rebuilds `GraphCache` immediately after, so a client polling
`/refresh-status` (or making a `/route` call) right after a `200` response
already sees the new data. There is no background task queue in this
project (CLAUDE.md: local, all-Python, no deployment concerns yet), so the
request simply blocks for the refresh's duration.

Two standing-rule protections, both from CLAUDE.md:
- **Refresh overlap guard**: `_refresh_lock` (in-process, `threading.Lock`)
  ensures a second `/refresh` request arriving while one is already
  running gets `409 Conflict` immediately rather than running concurrently
  with the first -- protects against double-hitting the external APIs and
  racing cache writes. Acquired non-blocking (`acquire(blocking=False)`) so
  a caller never queues/waits; it either starts immediately or is told to
  come back later.
- **Shared-secret gate**: `X-Refresh-Token` is checked against
  `settings.refresh_token` when one is configured; unset (the local-dev
  default) disables the check entirely.

`run_refresh_cycle()` (Phase 3) is the transport-agnostic "acquire lock,
run refresh, rebuild graph cache on success, release lock" sequence,
extracted out of `trigger_refresh` so `backend/main.py`'s background
auto-refresh scheduler can share the exact same overlap guard as a manual
`POST /refresh` -- a scheduled tick and a manual click can never run
concurrently, and a scheduled tick that loses the race just logs a skip
instead of the HTTP-only `409` a manual request gets. `trigger_refresh`
itself does the HTTP-layer translation (`raise HTTPException(409, ...)`)
on top of `run_refresh_cycle`'s plain result object; `run_refresh_cycle`
raises nothing HTTP-specific and has no `Request`/`Response` dependency.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from backend.config import Settings, get_settings
from backend.graph.cache import GraphCacheSnapshot, get_graph_cache
from backend.ingest.refresh import run_refresh
from backend.models.db import RefreshRun, session_scope
from backend.models.schemas import RefreshStatusOut
from backend.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

#: In-process only -- fine for this single-machine app (CLAUDE.md: no
#: multi-worker/multi-process deployment yet). A real multi-process
#: deployment would need a DB-backed or external lock instead.
_refresh_lock = threading.Lock()


class RefreshTriggerOutcome(str, Enum):
    """What happened when `run_refresh_cycle()` was called.

    `RAN` means `_refresh_lock` was acquired and `run_refresh()` actually
    executed -- check `RefreshTriggerResult.run.status` (`"success"` or
    `"failed"`) for the refresh's own outcome. `SKIPPED_OVERLAP` means
    another refresh already held the lock, so nothing ran at all -- this is
    a plain enum value, not an `HTTPException`, precisely so a non-HTTP
    caller (the scheduler) can branch on it without importing FastAPI.
    """

    RAN = "ran"
    SKIPPED_OVERLAP = "skipped_overlap"


@dataclass(frozen=True)
class RefreshTriggerResult:
    """Transport-agnostic outcome of one `run_refresh_cycle()` call.

    `run`/`snapshot` are populated iff `outcome is RefreshTriggerOutcome.RAN`
    (both `None` on `SKIPPED_OVERLAP` -- nothing ran, so there is no run row
    or fresh snapshot to report). `run` is a detached-safe SQLAlchemy object
    (`session_scope()` uses `expire_on_commit=False`), so its attributes
    remain readable after the session that fetched it has closed.
    """

    outcome: RefreshTriggerOutcome
    run: Optional[RefreshRun] = None
    snapshot: Optional[GraphCacheSnapshot] = None


def run_refresh_cycle(settings: Settings) -> RefreshTriggerResult:
    """Acquire `_refresh_lock` (non-blocking), run `run_refresh()`, rebuild
    `GraphCache` on success, release the lock -- exactly the sequence
    `trigger_refresh` used to run inline. Shared by the HTTP `/refresh`
    route and `backend/main.py`'s background auto-refresh scheduler so both
    triggers go through the identical overlap guard and can never run
    concurrently.

    Returns a `RefreshTriggerResult` rather than raising -- this function is
    deliberately transport-agnostic (no `HTTPException`, no `Request`); the
    HTTP-layer translation (`409` on overlap) happens in `trigger_refresh`.
    """
    if not _refresh_lock.acquire(blocking=False):
        return RefreshTriggerResult(outcome=RefreshTriggerOutcome.SKIPPED_OVERLAP)
    try:
        result = run_refresh(settings=settings)
        if result.success:
            get_graph_cache().rebuild(settings=settings)
        else:
            logger.warning("refresh run_id=%s failed, graph cache left unchanged", result.run_id)

        with session_scope() as session:
            run = session.get(RefreshRun, result.run_id)

        return RefreshTriggerResult(
            outcome=RefreshTriggerOutcome.RAN,
            run=run,
            snapshot=get_graph_cache().get_snapshot(),
        )
    finally:
        _refresh_lock.release()


def _verify_refresh_token(x_refresh_token: str | None, settings: Settings) -> None:
    """Plain helper (not a FastAPI `Depends`) called directly from
    `trigger_refresh` with the raw header value -- reading it straight off
    `request.headers` rather than via a `Header(...)`-annotated parameter
    keeps this trivially unit-testable without spinning up a `TestClient`.
    """
    if settings.refresh_token is None:
        return  # shared-secret gate disabled: local dev default
    if x_refresh_token != settings.refresh_token:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Refresh-Token header.")


def _run_to_status_out(run: RefreshRun, snapshot: GraphCacheSnapshot) -> RefreshStatusOut:
    return RefreshStatusOut(
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        commodities_count=run.commodities_count,
        terminals_count=run.terminals_count,
        prices_count=run.prices_count,
        distances_count=run.distances_count,
        ships_count=run.ships_count,
        error_message=run.error_message,
        data_version=snapshot.data_version,
        warnings=snapshot.warnings,
    )


@router.post("/refresh", response_model=RefreshStatusOut)
@limiter.limit(get_settings().rate_limit_refresh)
def trigger_refresh(request: Request) -> RefreshStatusOut:
    settings = get_settings()
    _verify_refresh_token(request.headers.get("X-Refresh-Token"), settings)

    result = run_refresh_cycle(settings)
    if result.outcome is RefreshTriggerOutcome.SKIPPED_OVERLAP:
        raise HTTPException(status_code=409, detail="A refresh is already in progress.")

    return _run_to_status_out(result.run, result.snapshot)


@router.get("/refresh-status", response_model=RefreshStatusOut)
@limiter.limit(get_settings().rate_limit_default)
def refresh_status(request: Request) -> RefreshStatusOut:
    snapshot = get_graph_cache().get_snapshot()
    with session_scope() as session:
        run = session.execute(select(RefreshRun).order_by(RefreshRun.id.desc()).limit(1)).scalars().first()
        if run is None:
            return RefreshStatusOut(
                status="never_run", data_version=snapshot.data_version, warnings=snapshot.warnings
            )
        return _run_to_status_out(run, snapshot)
