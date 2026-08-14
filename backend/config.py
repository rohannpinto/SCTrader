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
    uex_api_base_url: str = "https://uexcorp.space/api/2.0"
    uex_api_key: str | None = None

    # --- /refresh shared-secret gate (unset = disabled, local dev only) ---
    refresh_token: str | None = None

    # --- Route search / graph thresholds ---
    distance_threshold_default: float = 20000.0
    distance_threshold_max: float = 100000.0
    max_distance_cap: float = 500000.0
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
    search_label_cap_per_node: int = 50
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
