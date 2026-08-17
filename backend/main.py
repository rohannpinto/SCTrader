"""FastAPI application entrypoint: app assembly, CORS, rate limiting, and
the generic error handler. Run via `uvicorn backend.main:app --reload`
(see CLAUDE.md's "How to run").

Startup does two things before the app accepts traffic (via the `lifespan`
context manager, not the deprecated `@app.on_event`): `setup_logging()`
(must happen before anything else logs -- CLAUDE.md convention) and
`init_db()` + `GraphCache.rebuild()` to pre-warm the in-memory graph from
whatever's already on the on-disk cache DB, even before any `/refresh` has
run in this process (CLAUDE.md's two-tier caching architecture: "once at
backend startup to pre-warm from whatever's already on disk").

Phase 3 adds a background auto-refresh scheduler, started here (after the
pre-warm) when `settings.auto_refresh_enabled` is True: a daemon
`threading.Thread` that periodically calls the same transport-agnostic
`backend.routers.refresh.run_refresh_cycle()` a manual `POST /refresh`
uses, so both share `_refresh_lock` and can never run concurrently. A plain
`threading.Thread`, not an `asyncio.create_task`, mirrors the rest of this
project: `run_refresh`/`_refresh_lock` are synchronous, and the `/refresh`
route handler is a plain `def` specifically so Starlette runs it in its
worker threadpool without blocking the event loop -- reusing that same
synchronous machinery here avoids an unnecessary `asyncio.to_thread`
bridge. The loop waits on a `threading.Event` (never a bare `time.sleep`)
so shutdown can interrupt the wait immediately instead of blocking for up
to the full interval.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.config import Settings, get_settings
from backend.graph.cache import get_graph_cache
from backend.logging_config import setup_logging
from backend.models.db import init_db
from backend.rate_limit import limiter
from backend.routers import refresh as refresh_router
from backend.routers import route as route_router
from backend.routers import ships as ships_router
from backend.routers import terminals as terminals_router
from backend.routers.refresh import RefreshTriggerOutcome, run_refresh_cycle

setup_logging()
logger = logging.getLogger(__name__)

#: Name given to the scheduler thread purely so tests/diagnostics (e.g.
#: `threading.enumerate()`) can find it by name.
_SCHEDULER_THREAD_NAME = "auto-refresh-scheduler"

#: Bound on how long shutdown waits for the scheduler thread to notice the
#: stop event and exit its loop. The thread is `daemon=True` either way (so
#: it can never block process exit on its own), but a bounded `join()` here
#: still buys clean, deterministic shutdown logging/testability rather than
#: leaving a stray thread visibly running past `lifespan`'s shutdown log line.
_SCHEDULER_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0

#: Cap on any single `threading.Event.wait()` call the scheduler makes.
#: On this Windows Python build, `Event.wait()`'s `timeout` is bounded by
#: the underlying `WaitForSingleObject` API's DWORD-milliseconds parameter
#: (~4,294,967 seconds =~ 49.7 days) -- pass anything larger and it raises
#: an immediate, unhandled `OverflowError`, which would otherwise kill the
#: scheduler thread the moment it started waiting, silently (this app never
#: hooks `threading.excepthook`, so nothing would even get logged). One
#: hour is comfortably under that platform ceiling for *any* single wait
#: call regardless of how large `auto_refresh_interval_minutes` is
#: configured -- `_wait_for_interval_or_stop` below loops in chunks of at
#: most this size instead of a single call for the whole interval.
_MAX_SINGLE_WAIT_SECONDS = 3600.0  # 1 hour


def _wait_for_interval_or_stop(interval_seconds: float, stop_event: threading.Event) -> bool:
    """Waits up to `interval_seconds`, split into chunks no longer than
    `_MAX_SINGLE_WAIT_SECONDS` each so no individual `Event.wait()` call can
    ever approach the platform ceiling described above. Mirrors `Event.
    wait()`'s own return convention: True the moment `stop_event` is set
    (checked between every chunk, so shutdown reacts within one chunk's
    length -- at most `_MAX_SINGLE_WAIT_SECONDS` -- not the full interval);
    False once the entire `interval_seconds` has elapsed with no stop
    signal.

    Deliberately check-then-decide, not a `while remaining > 0:` guard up
    front: a `0` (or negative) `interval_seconds` must still call
    `stop_event.wait(timeout=0)` -- a real, if non-blocking, check of the
    flag -- exactly once, mirroring the pre-chunking code's `stop_event.
    wait(timeout=interval_seconds)` behavior for that same edge case. An
    earlier version of this function guarded the loop with `while remaining
    > 0`, which skipped the body (and therefore the stop-event check)
    entirely whenever `interval_seconds <= 0`, returning `False` having
    never looked at `stop_event` at all -- an infinite, un-interruptible
    busy loop back in `_auto_refresh_loop` for that configuration, caught by
    `test_auto_refresh_loop_fires_tick_then_stops_cleanly` (which uses
    `interval_seconds == 0` deliberately, to fire ticks without waiting out
    a real interval).
    """
    remaining = interval_seconds
    while True:
        chunk = min(remaining, _MAX_SINGLE_WAIT_SECONDS) if remaining > 0 else 0.0
        if stop_event.wait(timeout=chunk):
            return True
        remaining -= chunk
        if remaining <= 0:
            return False


def _auto_refresh_tick(settings: Settings) -> None:
    """One scheduler tick: call the shared refresh-trigger function and log
    the outcome. Split out from `_auto_refresh_loop` so a test can invoke a
    single tick directly and deterministically, without going through the
    thread's real wait/sleep timing at all.
    """
    logger.info("auto-refresh scheduler tick firing")
    try:
        result = run_refresh_cycle(settings)
    except Exception:
        # Defense in depth only -- `run_refresh_cycle`/`run_refresh` already
        # catch and record refresh failures themselves (CLAUDE.md: a failed
        # refresh reports `RefreshResult.status == "failed"` rather than
        # raising). This guards the scheduler loop against a genuinely
        # unexpected bug so one bad tick can never silently kill the thread.
        logger.exception("auto-refresh scheduler tick raised an unexpected exception")
        return

    if result.outcome is RefreshTriggerOutcome.SKIPPED_OVERLAP:
        logger.info("auto-refresh scheduler tick skipped: a refresh was already in progress")
    elif result.run is not None and result.run.status == "success":
        logger.info("auto-refresh scheduler tick succeeded: run_id=%d", result.run.id)
    else:
        logger.warning(
            "auto-refresh scheduler tick failed: run_id=%s",
            result.run.id if result.run is not None else None,
        )


def _auto_refresh_loop(settings: Settings, stop_event: threading.Event) -> None:
    """Runs in the background thread. Waits up to
    `settings.auto_refresh_interval_minutes * 60` seconds (interruptible by
    `stop_event`, via `_wait_for_interval_or_stop`'s chunking), fires one
    tick, repeats -- forever, until `stop_event` is set.

    The whole iteration is wrapped in a `try`/`except` as defense in depth
    one level up from `_auto_refresh_tick`'s own try/except around
    `run_refresh_cycle`: this only ever fires for a genuinely unexpected bug
    in the loop machinery itself (the wait helper, a future change, etc.),
    not for a normal failed refresh (which `_auto_refresh_tick` already
    catches and logs without raising). Without this, an uncaught exception
    here would kill the thread permanently with zero trace in the app's own
    logs -- this app never hooks `threading.excepthook`. Logged, then the
    loop keeps running (waits out the next interval and tries again) rather
    than dying silently.
    """
    interval_seconds = settings.auto_refresh_interval_minutes * 60
    logger.info("auto-refresh scheduler loop running: interval_seconds=%s", interval_seconds)
    while True:
        try:
            if _wait_for_interval_or_stop(interval_seconds, stop_event):
                logger.info("auto-refresh scheduler loop stopping")
                return
            _auto_refresh_tick(settings)
        except Exception:
            logger.exception(
                "auto-refresh scheduler loop raised an unexpected exception; will retry next interval"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("backend starting up")
    init_db()
    snapshot = get_graph_cache().rebuild()
    logger.info(
        "startup graph pre-warm: nodes=%d edges=%d data_version=%s",
        snapshot.graph.number_of_nodes(),
        snapshot.graph.number_of_edges(),
        snapshot.data_version,
    )

    settings = get_settings()
    stop_event = threading.Event()
    scheduler_thread: Optional[threading.Thread] = None
    if settings.auto_refresh_enabled:
        scheduler_thread = threading.Thread(
            target=_auto_refresh_loop,
            args=(settings, stop_event),
            name=_SCHEDULER_THREAD_NAME,
            daemon=True,
        )
        scheduler_thread.start()
        logger.info(
            "auto-refresh scheduler started: interval_minutes=%d",
            settings.auto_refresh_interval_minutes,
        )
    else:
        logger.info("auto-refresh scheduler disabled (auto_refresh_enabled=False)")

    yield

    logger.info("backend shutting down")
    stop_event.set()
    if scheduler_thread is not None:
        scheduler_thread.join(timeout=_SCHEDULER_SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        if scheduler_thread.is_alive():
            logger.warning("auto-refresh scheduler thread did not stop within shutdown timeout")
        else:
            logger.info("auto-refresh scheduler stopped")


app = FastAPI(
    title="Star Citizen Trading Route Optimizer",
    description="Computes efficient in-game commodity trading routes from live price/distance data.",
    lifespan=lifespan,
)

# --- rate limiting (slowapi) --------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# --- CORS: locked to the actual frontend origin, never "*" -------------------
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.cors_allowed_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- validation error handler: clean 422, never echo the raw rejected value --
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Replaces FastAPI's default `RequestValidationError` handler.

    Two problems with the default handler, both fixed here:

    1. **Bug (Task 17):** the default handler echoes the raw rejected value
       back into the response body (`error["input"]`) and serializes it via
       Starlette's `JSONResponse`, which calls `json.dumps(..., allow_nan=
       False)`. A non-finite float (`NaN`/`Infinity`/`-Infinity`) sent for
       *any* Pydantic-constrained numeric field (e.g. `RouteRequest.
       starting_budget`'s `ge=0`, `num_hops`'s `gt=0`) is correctly rejected
       by Pydantic, but that rejected value is exactly the kind of float
       `json.dumps` refuses to serialize -- raising `ValueError` from
       *inside* the default validation-error handler itself, which the
       generic `Exception` handler below then catches and turns into a
       `500`. Building the response body here with only the error `type`/
       `loc`/`msg` (never `error["input"]` or `error["ctx"]`, which can
       likewise hold a non-finite value) avoids ever handing a non-finite
       float to `JSONResponse`, so this always returns a clean `422`.
    2. **Hygiene (CLAUDE.md's security ground rules):** even setting the
       crash aside, echoing back exactly what the client submitted is more
       detail than a client needs -- dropping `input`/`ctx` is consistent
       with "never leak more internal detail than necessary."
    """
    errors = [
        {"type": error["type"], "loc": error["loc"], "msg": error["msg"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


# --- generic error handler: never leak internal exception details ------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches anything not already handled by a more specific handler
    (`HTTPException`, `RequestValidationError`, and `RateLimitExceeded`
    above all keep their own normal handling -- Starlette dispatches to the
    most specific registered handler for the exception type, so this one
    only ever sees genuinely unexpected errors). Per CLAUDE.md's security
    ground rules: never return a raw stack trace or exception string to the
    client -- log the real exception server-side, return a fixed, generic
    message.
    """
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "An internal error occurred."})


@app.get("/health")
def health() -> dict[str, str]:
    """Unauthenticated, unthrottled liveness check -- no data, safe to poll freely."""
    return {"status": "ok"}


app.include_router(terminals_router.router)
app.include_router(refresh_router.router)
app.include_router(route_router.router)
app.include_router(ships_router.router)
