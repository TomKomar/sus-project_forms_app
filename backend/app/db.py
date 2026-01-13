# ---------------------------------------------------------------------------
# db.py
#
# SQLAlchemy database setup.
#
# This module defines:
# - `engine`: the SQLAlchemy Engine (connection pool and DB driver)
# - `SessionLocal`: the session factory used for per-request sessions
# - `Base`: the declarative base class for ORM models
#
# All modules should import database primitives from this file to ensure
# consistent connection pooling and transaction semantics.
# ---------------------------------------------------------------------------

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

# pool_pre_ping proactively checks connections to avoid stale sockets.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

# Disable autocommit/autoflush to make writes explicit and predictable.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""

    pass
