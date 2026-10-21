"""Conexión. SQLite local por defecto; PostgreSQL si hay DATABASE_URL."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_URL = f"sqlite:///{DATA / 'kinkara.db'}"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def build_engine(url: str | None = None) -> Engine:
    target = url or database_url()
    kwargs: dict = {"future": True}
    if target.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if target in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    return create_engine(target, **kwargs)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    if connection_record and type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def session_factory(engine: Engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
