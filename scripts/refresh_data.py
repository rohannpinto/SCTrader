"""CLI entrypoint for a manual, one-off cache refresh.

Usage (from the project root, venv activated):

    python scripts/refresh_data.py

Equivalent to `POST /refresh` against a running backend, but doesn't need
the backend process to be up -- runs the same `run_refresh()` pipeline
(`backend/ingest/refresh.py`) directly against the on-disk cache DB. This
process holds no in-memory `GraphCache` of its own to rebuild (there's no
running backend here) -- the backend picks up whatever this script wrote
to disk the next time it starts (startup pre-warm) or the next time an
operator hits `POST /refresh`.

Exit code is `0` on a successful refresh, `1` on failure, so this is safe
to drive from a cron job or scheduled task that should alert on failure.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# `python scripts/refresh_data.py` (run directly, not `python -m ...`, per
# this docstring's own documented usage and CLAUDE.md's "How to run") sets
# sys.path[0] to this script's own directory (`scripts/`), not the project
# root -- so `backend` isn't importable without adding the root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.ingest.refresh import run_refresh  # noqa: E402
from backend.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    logger.info("manual refresh starting")

    result = run_refresh(settings=get_settings())

    if result.success:
        logger.info(
            "manual refresh succeeded: terminals=%d commodities=%d prices=%d distances=%d",
            result.terminals_count,
            result.commodities_count,
            result.prices_count,
            result.distances_count,
        )
        return 0

    logger.error("manual refresh failed: %s", result.error_message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
