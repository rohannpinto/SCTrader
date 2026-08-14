"""Unit tests for `backend/models/db.py`.

All tests use a temp *file* SQLite database (never `:memory:`) because WAL
mode requires a real on-disk file -- SQLite silently keeps the default
rollback-journal mode for `:memory:` databases, which would make the WAL
assertion meaningless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models.db import (
    Commodity,
    Distance,
    Price,
    RefreshRun,
    Terminal,
    create_db_engine,
    get_current_data_version,
    init_db,
    session_scope,
)


@pytest.fixture
def engine(tmp_path):
    """A SQLite engine bound to a temp *file* DB, with tables created."""
    db_file = tmp_path / "test_cache.db"
    eng = create_db_engine(str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- table creation -----------------------------------------------------


def test_init_db_creates_all_tables(engine):
    from sqlalchemy import inspect

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {"commodities", "terminals", "prices", "distances", "refresh_runs"} <= table_names


# --- WAL mode -------------------------------------------------------------


def test_wal_mode_is_active_on_live_connection(engine):
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert result is not None
    assert result.lower() == "wal"


# --- foreign key enforcement ---------------------------------------------


def test_foreign_keys_pragma_is_on(engine):
    with engine.connect() as conn:
        result = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert result == 1


def test_foreign_keys_reapplied_on_every_new_physical_connection(engine):
    # `foreign_keys` is a per-connection SQLite setting (unlike WAL, which
    # is persisted in the database file) -- it resets to OFF on every new
    # physical DBAPI connection unless the connect-event listener re-applies
    # it. Force brand-new physical connections via engine.dispose() between
    # checks so this actually exercises "every new connection", not just
    # pooled reuse of the same physical connection.
    for _ in range(3):
        engine.dispose()
        with engine.connect() as conn:
            result = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
            assert result == 1


def test_foreign_key_violation_is_rejected(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            # No Terminal or Commodity rows exist yet -- this must fail.
            session.add(
                Price(
                    terminal_id=999,
                    commodity_id=999,
                    price_buy=10.0,
                    price_sell=20.0,
                    fetched_at=_now(),
                )
            )


# --- Commodity uniqueness -------------------------------------------------


def test_commodity_wiki_uuid_uniqueness_enforced(engine):
    with session_scope(engine) as session:
        session.add(Commodity(wiki_uuid="uuid-1", slug="laranite", name="Laranite"))

    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(Commodity(wiki_uuid="uuid-1", slug="laranite-2", name="Laranite Dup"))


# --- Price composite PK ---------------------------------------------------


def test_price_composite_pk_rejects_duplicate(engine):
    with session_scope(engine) as session:
        session.add(Commodity(id=1, wiki_uuid="uuid-1", slug="laranite", name="Laranite"))
        session.add(Terminal(id=1, name="Terminal A"))
        session.add(Terminal(id=2, name="Terminal B"))

    with session_scope(engine) as session:
        session.add(
            Price(
                terminal_id=1,
                commodity_id=1,
                price_buy=10.0,
                price_sell=20.0,
                fetched_at=_now(),
            )
        )

    # Duplicate (terminal_id, commodity_id) pair -- rejected, not upserted.
    # This project stores current-only prices; a refresh is expected to
    # DELETE + re-INSERT (or otherwise clear) prior rows rather than rely on
    # the ORM to silently overwrite on primary-key collision.
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(
                Price(
                    terminal_id=1,
                    commodity_id=1,
                    price_buy=11.0,
                    price_sell=21.0,
                    fetched_at=_now(),
                )
            )


# --- Distance composite PK ------------------------------------------------


def test_distance_composite_pk_rejects_duplicate(engine):
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Terminal A"))
        session.add(Terminal(id=2, name="Terminal B"))

    with session_scope(engine) as session:
        session.add(
            Distance(terminal_a_id=1, terminal_b_id=2, distance=100.0, fetched_at=_now())
        )

    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(
                Distance(terminal_a_id=1, terminal_b_id=2, distance=999.0, fetched_at=_now())
            )


def test_distance_is_directed_not_assumed_symmetric(engine):
    # (a, b) and (b, a) are independent rows -- both inserts must succeed.
    with session_scope(engine) as session:
        session.add(Terminal(id=1, name="Terminal A"))
        session.add(Terminal(id=2, name="Terminal B"))

    with session_scope(engine) as session:
        session.add(Distance(terminal_a_id=1, terminal_b_id=2, distance=100.0, fetched_at=_now()))
        session.add(Distance(terminal_a_id=2, terminal_b_id=1, distance=150.0, fetched_at=_now()))

    with session_scope(engine) as session:
        rows = session.query(Distance).all()
        assert len(rows) == 2


# --- data version helper ---------------------------------------------------


def test_data_version_none_when_no_runs(engine):
    with session_scope(engine) as session:
        assert get_current_data_version(session) is None


def test_data_version_none_with_only_failed_and_running_runs(engine):
    base_time = _now()
    with session_scope(engine) as session:
        session.add(
            RefreshRun(started_at=base_time, completed_at=base_time, status="failed")
        )
        session.add(RefreshRun(started_at=base_time + timedelta(minutes=1), status="running"))

    with session_scope(engine) as session:
        assert get_current_data_version(session) is None


def test_data_version_returns_success_run_id(engine):
    base_time = _now()
    with session_scope(engine) as session:
        session.add(
            RefreshRun(started_at=base_time, completed_at=base_time, status="failed")
        )
        session.add(
            RefreshRun(
                started_at=base_time + timedelta(minutes=1),
                completed_at=base_time + timedelta(minutes=2),
                status="success",
            )
        )

    with session_scope(engine) as session:
        version = get_current_data_version(session)
        success_run = session.query(RefreshRun).filter_by(status="success").one()
        assert version == success_run.id


def test_data_version_returns_newest_success_run_id(engine):
    base_time = _now()
    with session_scope(engine) as session:
        session.add(
            RefreshRun(
                started_at=base_time,
                completed_at=base_time,
                status="success",
            )
        )

    with session_scope(engine) as session:
        first_version = get_current_data_version(session)

    with session_scope(engine) as session:
        session.add(
            RefreshRun(
                started_at=base_time + timedelta(hours=1),
                completed_at=base_time + timedelta(hours=1, minutes=5),
                status="success",
            )
        )

    with session_scope(engine) as session:
        second_version = get_current_data_version(session)

    assert second_version is not None
    assert first_version is not None
    assert second_version > first_version


# --- session_scope transactional behavior ----------------------------------


def test_session_scope_commits_on_clean_exit(engine):
    with session_scope(engine) as session:
        session.add(Commodity(wiki_uuid="uuid-x", slug="agricium", name="Agricium"))

    with session_scope(engine) as session:
        assert session.query(Commodity).filter_by(slug="agricium").one_or_none() is not None


def test_session_scope_rolls_back_on_exception(engine):
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with session_scope(engine) as session:
            session.add(Commodity(wiki_uuid="uuid-y", slug="titanium", name="Titanium"))
            session.flush()
            raise _Boom("simulated failure")

    with session_scope(engine) as session:
        assert session.query(Commodity).filter_by(slug="titanium").one_or_none() is None
