"""SQLAlchemy 2.0 declarative models for the local SQLite cache database.

This module owns the on-disk "source of truth" tier of the two-tier caching
architecture described in `CLAUDE.md`: a small SQLite database
(`settings.db_path`, WAL mode) that the (future) ingestion pipeline
populates from the two external API clients, and that the (future) graph
builder bulk-loads from to construct the in-memory `networkx.DiGraph`.

Schema
------
- `Commodity`   -- one row per tradeable commodity (keyed by the wiki's UUID).
- `Terminal`    -- one row per trade-capable terminal, keyed by a clean
                   internal surrogate `id`. The wiki and UEX APIs use their
                   own, not-necessarily-matching terminal identifiers; those
                   are stored as separate nullable, unique columns
                   (`wiki_terminal_id`, `uex_terminal_id`) so only the
                   ingestion task ever has to reconcile them. Every other
                   part of the app refers to terminals solely by the
                   internal `id`.
- `Price`       -- current known buy/sell price for one commodity at one
                   terminal (no history kept). Composite primary key
                   `(terminal_id, commodity_id)` -- this *is* the composite
                   index CLAUDE.md calls for on `prices(terminal_id,
                   commodity_id)`; SQLite creates its PK index for free.
- `Distance`    -- a directed distance between two terminals, stored exactly
                   as the source API reports it (no assumed symmetry).
                   Composite primary key `(terminal_a_id, terminal_b_id)`
                   likewise doubles as the required composite index.
- `RefreshRun`  -- one row per data-refresh attempt. Its autoincrement `id`
                   doubles as the monotonic "data version" used later for
                   graph-cache invalidation and the `/route` LRU cache key.

Engine / session management
----------------------------
- `create_db_engine(db_path=None)` builds a SQLite engine pointed at
  `settings.db_path` (or an explicit override, e.g. a temp file in tests),
  creating the parent directory if needed, and registers a `connect` event
  listener on that specific engine so that **every** new DBAPI connection
  (not just the first) gets `PRAGMA journal_mode=WAL` and
  `PRAGMA foreign_keys=ON` applied.
- `get_engine()` / `get_session_factory()` are a lazy, settings-backed
  singleton pair for normal app use.
- `session_scope()` is a context manager for transactional use: commits on
  clean exit, rolls back on exception, always closes.
- `init_db(engine=None)` calls `Base.metadata.create_all(...)` -- this
  project intentionally has no migration framework.
- `get_current_data_version(session)` returns the `id` of the most recent
  `RefreshRun` row with `status == "success"`, or `None` if there isn't one.

Note on WAL mode and `:memory:` databases: WAL mode requires a real on-disk
file. SQLite silently keeps the default rollback-journal mode for
`:memory:` databases, so any test asserting WAL mode must use a temp file
database, not `:memory:`.

Security note: all access in this module (and everywhere else in the app)
goes through the SQLAlchemy ORM's parameterized query APIs. The only
non-ORM SQL touching a live connection here is the fixed, hardcoded
`PRAGMA` statements used to configure the connection itself -- static
strings with zero user input, not data queries.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    String,
    Boolean,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from backend.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base class for every ORM model in this app."""


class Commodity(Base):
    """A tradeable commodity, keyed by its api.star-citizen.wiki UUID."""

    __tablename__ = "commodities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wiki_uuid: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Commodity(id={self.id!r}, slug={self.slug!r})"


class Terminal(Base):
    """A single trade-capable terminal (graph node).

    `id` is a clean internal surrogate key, deliberately decoupled from the
    external APIs' own terminal identifiers -- see module docstring.
    """

    __tablename__ = "terminals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wiki_terminal_id: Mapped[Optional[int]] = mapped_column(
        Integer, unique=True, index=True, nullable=True
    )
    uex_terminal_id: Mapped[Optional[int]] = mapped_column(
        Integer, unique=True, index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    star_system_name: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    location_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_commodity_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Terminal(id={self.id!r}, name={self.name!r})"


class Price(Base):
    """Current known buy/sell price for one commodity at one terminal.

    No price history is kept -- a refresh overwrites the row for a given
    `(terminal_id, commodity_id)` pair (see ingestion task for the actual
    upsert; this task only defines the constraint shape).
    """

    __tablename__ = "prices"

    terminal_id: Mapped[int] = mapped_column(ForeignKey("terminals.id"), primary_key=True)
    commodity_id: Mapped[int] = mapped_column(ForeignKey("commodities.id"), primary_key=True)
    price_buy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_sell: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_date_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"Price(terminal_id={self.terminal_id!r}, "
            f"commodity_id={self.commodity_id!r})"
        )


class Distance(Base):
    """A directed distance between two terminals, stored as-reported.

    Symmetry is never assumed: `(a, b)` and `(b, a)` are independent rows.
    """

    __tablename__ = "distances"

    terminal_a_id: Mapped[int] = mapped_column(ForeignKey("terminals.id"), primary_key=True)
    terminal_b_id: Mapped[int] = mapped_column(ForeignKey("terminals.id"), primary_key=True)
    distance: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"Distance(terminal_a_id={self.terminal_a_id!r}, "
            f"terminal_b_id={self.terminal_b_id!r})"
        )


#: Allowed values for `RefreshRun.status`. Enforced both here (documentation
#: / type hint) and at the DB level via a CHECK constraint.
REFRESH_RUN_STATUSES = ("running", "success", "failed")


class RefreshRun(Base):
    """Tracks a single data-refresh attempt.

    `id` doubles as the monotonic "data version": the graph cache and the
    `/route` LRU cache key both use the `id` of the most recent successful
    run to know when the underlying data has changed.
    """

    __tablename__ = "refresh_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="ck_refresh_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    commodities_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    terminals_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prices_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distances_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"RefreshRun(id={self.id!r}, status={self.status!r})"


def _resolve_db_path(db_path: Optional[str]) -> Path:
    """Resolve the configured/overridden db path to an absolute `Path`.

    Uses `settings.db_path` when `db_path` is not given. Resolves to an
    absolute path so the resulting `sqlite:///...` URL is unambiguous
    regardless of the process's current working directory, and so Windows
    drive letters serialize correctly (forward slashes, three leading
    slashes plus the drive).
    """
    raw = db_path if db_path is not None else get_settings().db_path
    return Path(raw).resolve()


def create_db_engine(db_path: Optional[str] = None) -> Engine:
    """Create a SQLite engine for the cache DB.

    Creates the parent directory for the db file if it doesn't exist yet.
    Registers a `connect` event listener on *this specific engine* so that
    `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` are (re-)applied
    on every new DBAPI connection the engine opens -- not just once at
    startup, which would be lost if the connection pool cycles connections.
    """
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        # Fixed, hardcoded PRAGMA strings only -- no user input, no string
        # formatting. Not representable through the ORM query API, which is
        # for data access; this is connection configuration.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    logger.info("db engine created path=%s", path)
    return engine


_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker[Session]] = None


def get_engine() -> Engine:
    """Return the process-wide, lazily-created engine bound to `settings.db_path`."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Return a session factory bound to `engine`, or the default singleton engine.

    When an explicit `engine` is given (e.g. a test's temp-file engine), a
    fresh, uncached `sessionmaker` bound to it is returned so tests never
    accidentally share the process-wide default engine/session factory.
    """
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope(engine: Optional[Engine] = None) -> Iterator[Session]:
    """Provide a transactional session: commits on success, rolls back on error.

    Always closes the session on exit. Pass `engine` explicitly in tests to
    target a temp-file database instead of the process-wide default.
    """
    factory = get_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Create all tables that don't already exist. No migration framework.

    Returns the engine used, so callers can chain further setup.
    """
    target_engine = engine if engine is not None else get_engine()
    Base.metadata.create_all(target_engine)
    logger.info("db initialized")
    return target_engine


def get_current_data_version(session: Session) -> Optional[int]:
    """Return the `id` of the most recent successful `RefreshRun`, or `None`.

    "Most recent" means highest `id` among rows with `status == "success"`;
    `RefreshRun.id` is monotonically increasing and doubles as the data
    version, so this is equivalent to and simpler than ordering by a
    timestamp.
    """
    stmt = (
        select(RefreshRun.id)
        .where(RefreshRun.status == "success")
        .order_by(RefreshRun.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()
