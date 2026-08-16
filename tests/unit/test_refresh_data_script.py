"""Unit tests for `scripts/refresh_data.py`'s `main()`.

Loaded directly by file path via `importlib` rather than a normal package
import -- `scripts/` is a standalone CLI entrypoint directory, deliberately
not an importable package (see `backend/graph/__init__.py`-style modules
for the contrast; CLAUDE.md's project structure lists `scripts/
refresh_data.py` as a lone file). `module.__name__` is set to something
other than `"__main__"` here, so the script's own `if __name__ ==
"__main__": sys.exit(main())` guard never fires on import -- `main()` is
called explicitly by each test instead.

Isolation strategy
----------------------
`main()` never passes an explicit `engine=` to `run_refresh()` / `init_db()`
-- it always operates against `backend.models.db`'s process-wide, lazily
created default engine (bound to `settings.db_path`, which defaults to the
real project's `./data/cache.db`). The autouse `_isolated_db` fixture below
points that singleton at a fresh temp-file DB for every test in this
module (same monkeypatching strategy as `tests/integration/test_api.py`),
so nothing here ever touches the real on-disk cache.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest
import respx
from sqlalchemy import inspect

import backend.models.db as db_module

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_data.py"


@dataclass
class _FakeRefreshResult:
    status: str
    terminals_count: Optional[int] = None
    commodities_count: Optional[int] = None
    prices_count: Optional[int] = None
    distances_count: Optional[int] = None
    ships_count: Optional[int] = None
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "success"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point `backend.models.db`'s default engine at a fresh temp DB.

    Deliberately does **not** call `init_db()` here -- unlike other test
    modules' `engine` fixtures, tests in this file need to control for
    themselves whether the schema exists yet before `main()` runs (that's
    exactly what the fresh-checkout regression test below exercises).
    """
    db_file = tmp_path / "refresh_data_script_test.db"
    eng = db_module.create_db_engine(str(db_file))
    monkeypatch.setattr(db_module, "_engine", eng)
    monkeypatch.setattr(db_module, "_session_factory", None)
    yield eng
    eng.dispose()


@pytest.fixture
def script_module():
    spec = importlib.util.spec_from_file_location("refresh_data_script_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_returns_zero_and_logs_counts_on_success(script_module, monkeypatch, caplog):
    fake_result = _FakeRefreshResult(
        status="success",
        terminals_count=10,
        commodities_count=5,
        prices_count=40,
        distances_count=90,
        ships_count=8,
    )
    monkeypatch.setattr(script_module, "run_refresh", lambda settings: fake_result)

    with caplog.at_level("INFO"):
        exit_code = script_module.main()

    assert exit_code == 0
    assert any("succeeded" in message for message in caplog.messages)


def test_main_returns_one_and_logs_error_on_failure(script_module, monkeypatch, caplog):
    fake_result = _FakeRefreshResult(status="failed", error_message="external API unreachable")
    monkeypatch.setattr(script_module, "run_refresh", lambda settings: fake_result)

    with caplog.at_level("ERROR"):
        exit_code = script_module.main()

    assert exit_code == 1
    assert any("external API unreachable" in message for message in caplog.messages)


# --- fresh-checkout regression guard -----------------------------------------
#
# Reproduces the exact bug: `main()` used to call `run_refresh()` without
# ever calling `init_db()` first. `run_refresh()` inserts its "started"
# `RefreshRun` bookkeeping row *before* its own try/except (by design, so
# that specific failure isn't silently swallowed), so on a genuinely fresh
# DB (no tables yet) that insert raised a raw, unhandled
# `sqlalchemy.exc.OperationalError: no such table: refresh_runs` straight
# out of `main()` -- never reaching this script's own `logger.error(...)` +
# `return 1` failure path.


def test_main_calls_init_db_before_run_refresh(script_module, monkeypatch):
    """Ordering guard: `init_db()` must run before `run_refresh()` is ever
    invoked, mirroring `backend/main.py`'s lifespan ordering."""
    call_order: list[str] = []
    fake_result = _FakeRefreshResult(
        status="success",
        terminals_count=1,
        commodities_count=1,
        prices_count=1,
        distances_count=1,
        ships_count=1,
    )
    monkeypatch.setattr(script_module, "init_db", lambda: call_order.append("init_db"))
    monkeypatch.setattr(
        script_module,
        "run_refresh",
        lambda settings: (call_order.append("run_refresh"), fake_result)[1],
    )

    exit_code = script_module.main()

    assert exit_code == 0
    assert call_order == ["init_db", "run_refresh"]


def test_main_on_fresh_db_creates_schema_and_does_not_crash(script_module, caplog):
    """The exact reported scenario: no DB file/tables exist before `main()`
    runs (`_isolated_db` binds a brand-new temp file, and -- unlike every
    other fixture's `engine` in this codebase -- deliberately never calls
    `init_db()` ahead of time). `run_refresh()` itself is not mocked, only
    the outbound HTTP calls it would make are left unmocked via an empty
    `respx.mock` context, so a real "no schema yet" DB is exercised without
    hitting the real network.

    Before the fix: raw `OperationalError: no such table: refresh_runs`
    escaping `main()`. After the fix: `init_db()` creates the schema first,
    `run_refresh()`'s bookkeeping insert succeeds, and the refresh then
    fails cleanly for an unrelated reason (no mocked HTTP responses) through
    the script's own structured-logging + exit-code-1 path.
    """
    inspector = inspect(db_module.get_engine())
    assert "refresh_runs" not in inspector.get_table_names(), "fixture must start with no schema"

    with caplog.at_level("INFO"):
        with respx.mock:  # active, but nothing registered: any HTTP call fails cleanly
            exit_code = script_module.main()

    # Never a raw unhandled traceback out of main() -- always a clean int.
    assert exit_code == 1
    assert not any("no such table" in message.lower() for message in caplog.messages)
    assert any("db initialized" in message.lower() for message in caplog.messages)
    assert any("manual refresh failed" in message for message in caplog.messages)

    # The schema now exists even though the refresh itself failed -- proves
    # init_db() ran and succeeded, and that the failure was in the fetch
    # phase (no mocked responses), not a database-schema failure.
    inspector = inspect(db_module.get_engine())
    assert "refresh_runs" in inspector.get_table_names()


def test_main_run_twice_against_same_fresh_db_is_idempotent(script_module, monkeypatch, caplog):
    """`init_db()` (`Base.metadata.create_all`) only creates missing tables
    -- running the script twice in a row against the same temp DB (second
    run now non-fresh) must behave identically both times, not error or
    duplicate schema objects on the second call."""
    fake_result = _FakeRefreshResult(
        status="success",
        terminals_count=1,
        commodities_count=1,
        prices_count=1,
        distances_count=1,
        ships_count=1,
    )
    monkeypatch.setattr(script_module, "run_refresh", lambda settings: fake_result)

    with caplog.at_level("INFO"):
        first_exit_code = script_module.main()
        second_exit_code = script_module.main()

    assert first_exit_code == 0
    assert second_exit_code == 0
    assert sum("succeeded" in message for message in caplog.messages) == 2

    inspector = inspect(db_module.get_engine())
    assert "refresh_runs" in inspector.get_table_names()
