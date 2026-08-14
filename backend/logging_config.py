"""Structured logging setup.

Call `setup_logging()` once, early in the application entrypoint (backend
`main.py`, `scripts/refresh_data.py`, etc.) before any other module logs.
Every module in this codebase must use `logging.getLogger(__name__)` and
never `print()` — see CLAUDE.md conventions.
"""

import logging
import sys

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a consistent structured format.

    Idempotent: safe to call more than once (e.g. once from `main.py` and
    once from a test fixture) without duplicating handlers.

    Format per line: `timestamp | LEVEL | logger.name | message`. When
    logging key-value context, prefer `logger.info("event key=%s", value)`
    style formatting (lazy `%` args, not f-strings) so the message is only
    built when the log level is enabled, and never interpolate raw,
    untrusted user input directly into the message string.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)

    _configured = True
