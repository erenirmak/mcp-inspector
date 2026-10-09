"""
Database layer — SQLAlchemy ORM with PostgreSQL (primary) / SQLite (fallback).

Usage:
    from backend.db import init_db, get_db, ServerModel
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import Boolean, Column, Float, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

try:
    from sqlalchemy import JSON

    _JSON = JSON
except ImportError:  # very old SA builds
    from sqlalchemy import Text as _JSON  # type: ignore[assignment]

log = logging.getLogger(__name__)

# ── ORM base ─────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class ServerModel(Base):
    """Persisted MCP server record."""

    __tablename__ = "servers"

    id = Column(String(64), primary_key=True)
    name = Column(String(256), nullable=False)
    url = Column(Text, nullable=False)
    conn_type = Column(String(16), default="http")
    online = Column(Boolean, default=False)  # this might not be suitable for postgres backend
    last_checked = Column(Float, default=0.0)  # this might not be suitable for postgres backend
    last_error = Column(Text, default="")  # this might not be suitable for postgres backend
    tools = Column(_JSON, default=list, nullable=False)
    resources = Column(_JSON, default=list, nullable=False)
    prompts = Column(_JSON, default=list, nullable=False)
    command = Column(Text, default="")
    args = Column(_JSON, default=list, nullable=False)


def _migrate_add_columns(engine, is_postgres: bool) -> None:
    """Add columns that ``create_all`` cannot add to an existing table.

    ``create_all`` only creates missing tables; it never alters existing ones.
    This lightweight migration adds the ``command``/``args`` columns to a
    pre-existing ``servers`` table (SQLite and PostgreSQL).
    """
    from sqlalchemy import inspect, text

    try:
        existing = {c["name"] for c in inspect(engine).get_columns("servers")}
    except Exception:
        return  # table doesn't exist yet (fresh DB) — create_all handled it

    with engine.begin() as conn:
        if "command" not in existing:
            conn.execute(text("ALTER TABLE servers ADD COLUMN command TEXT DEFAULT ''"))
            log.info("Migration: added servers.command column")
        if "args" not in existing:
            col_type = "JSON" if is_postgres else "JSON"
            conn.execute(text(f"ALTER TABLE servers ADD COLUMN args {col_type} DEFAULT '[]'"))
            log.info("Migration: added servers.args column")


# ── Engine / session factory ──────────────────────────────────────────────────

_engine = None
_SessionLocal = None


def init_db(database_url: str) -> None:
    """Initialise the database engine and create tables.

    Call once at application startup before any DB operations.
    """
    global _engine, _SessionLocal

    is_postgres = database_url.startswith("postgresql")

    try:
        if is_postgres:
            _engine = create_engine(
                database_url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,  # detect stale connections
                future=True,
            )
            log.info("Connected to PostgreSQL.")
        else:
            _engine = create_engine(
                database_url,
                connect_args={"check_same_thread": False},
                future=True,
            )

            # Enable WAL mode on SQLite for better concurrent read performance
            @event.listens_for(_engine, "connect")
            def _set_wal(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
                dbapi_conn.execute("PRAGMA synchronous=NORMAL")

            log.info("Using SQLite: %s", database_url)

        Base.metadata.create_all(_engine)
        _migrate_add_columns(_engine, is_postgres)
        _SessionLocal = sessionmaker(
            bind=_engine,
            autocommit=False,
            autoflush=False,
            future=True,
        )
        log.info("Database tables ready.")

    except Exception:
        log.exception("Failed to initialise database.")
        raise


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager that yields a scoped Session and handles commit/rollback."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialised — call init_db() first.")
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
