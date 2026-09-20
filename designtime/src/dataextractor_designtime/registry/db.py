"""Engine and session plumbing for the Postgres registry."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_settings


class Base(DeclarativeBase):
    pass


#: Engines are cached per URL: a request that names its database should reuse
#: the pool, not build a new one on every call.
_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker[Session]] = {}


def get_engine(url: str | None = None, *, force: bool = False) -> Engine:
    target = url or get_settings().database_url
    if force or target not in _engines:
        engine = create_engine(target, pool_pre_ping=True, future=True)
        _engines[target] = engine
        _sessionmakers[target] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _engines[target]


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    target = url or get_settings().database_url
    get_engine(target)
    return _sessionmakers[target]


def dispose_all() -> None:
    """Close every pooled connection. Used by tests between databases."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _sessionmakers.clear()


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    factory = get_sessionmaker(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
