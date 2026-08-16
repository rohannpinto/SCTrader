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
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.config import get_settings
from backend.graph.cache import get_graph_cache
from backend.logging_config import setup_logging
from backend.models.db import init_db
from backend.rate_limit import limiter
from backend.routers import refresh as refresh_router
from backend.routers import route as route_router
from backend.routers import ships as ships_router
from backend.routers import terminals as terminals_router

setup_logging()
logger = logging.getLogger(__name__)


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
    yield
    logger.info("backend shutting down")


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
