"""Application configuration.

Loads settings from environment variables / a local `.env` file via
`pydantic-settings`. Access the singleton via `get_settings()` rather than
instantiating `Settings()` directly, so the whole app shares one parsed,
validated configuration object.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the backend.

    Every field can be overridden via an environment variable of the same
    name (case-insensitive) or a `.env` file in the project root. See
    `.env.example` for the full list with descriptions.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- External API endpoints ---
    wiki_api_base_url: str = "https://api.star-citizen.wiki/api"
    # Verified empirically (2026-08-14, Task 3) -- see `backend/clients/
    # uex_client.py`'s module docstring. `uexcorp.space/api/2.0` (the
    # original placeholder here) fronts the exact same API but has
    # Cloudflare bot-management enabled that blocks Python `httpx` clients
    # (TLS/HTTP fingerprinting, not a UA-string check -- survives even a
    # full browser-like header set). `api.uexcorp.uk` is UEX's other public
    # domain for the identical API/dataset and has no such block; `httpx`
    # reaches it with zero special headers. No API key is required for the
    # endpoints this app uses (`terminals`, `terminals_distances`,
    # `star_systems`) -- confirmed via UEX's own docs (no "Bearer
    # Authorization Required" lock icon on those three) and by successful
    # anonymous requests.
    uex_api_base_url: str = "https://api.uexcorp.uk/2.0"
    uex_api_key: str | None = None

    # --- /refresh shared-secret gate (unset = disabled, local dev only) ---
    refresh_token: str | None = None

    # --- Route search / graph thresholds ---
    # Phase 2: the continuous distance-budget model (distance_threshold_default/
    # _max, max_distance_cap) is retired -- a route's per-hop distance filter is
    # now the *selected ship's* quantum_range_gm (resolved server-side from
    # ship_id), not a client-supplied distance parameter. See CLAUDE.md's "Route
    # search problem" section.
    max_hops_cap: int = 50
    """Server-side cap on `RouteRequest.num_hops`, enforced regardless of what
    the client claims (CLAUDE.md's security ground rules). The Phase 2 DP's
    state space is `O(nodes * num_hops)`, already small and exactly bounded --
    this cap exists to bound worst-case latency/memory on a request, not
    because the algorithm needs it to terminate."""
    max_starting_budget_cap: float = 100_000_000.0
    """Server-side cap on `RouteRequest.starting_budget` (aUEC), enforced
    regardless of what the client claims. Generous relative to realistic
    in-game cash balances -- exists purely as a sane upper bound against
    malformed/adversarial input, not a realistic gameplay limit."""
    min_distance_floor: float = 1.0

    # --- Storage ---
    db_path: str = "./data/cache.db"

    # --- CORS ---
    cors_allowed_origin: str = "http://localhost:8501"

    # --- Rate limiting (slowapi-style strings, e.g. "60/minute") ---
    rate_limit_default: str = "60/minute"
    rate_limit_refresh: str = "5/minute"
    rate_limit_route: str = "20/minute"

    # --- Shared HTTP client policy (wiki_client + uex_client) ---
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3

    # --- Graph build / search safety valves ---
    graph_edge_count_guardrail: int = 50000
    search_time_budget_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached `Settings` instance.

    `lru_cache` with no arguments makes this a memoized singleton: the
    first call parses env/.env, every subsequent call returns the same
    object. Tests that need different settings should call
    `get_settings.cache_clear()` after monkeypatching the environment.
    """
    return Settings()
