"""Shared FastAPI dependencies and error translation."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..agents.base import AgentError
from ..config import Settings, get_settings
from ..model.base import ModelClient
from ..model.stub import StubModelClient
from ..registry.db import get_sessionmaker
from ..registry.errors import RegistryError
from ..registry.repository import Registry


def settings_dep() -> Settings:
    return get_settings()


def model_dep(request: Request) -> ModelClient:
    """Lets a test or a deployment swap the judgment backend in one place."""
    return getattr(request.app.state, "model_client", None) or StubModelClient()


def session_dep(request: Request) -> Iterator[Session]:
    url = getattr(request.app.state, "database_url", None)
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


def registry_dep(session: Session = Depends(session_dep)) -> Registry:
    return Registry(session)


def as_http(exc: Exception) -> HTTPException:
    """Registry and agent errors carry their own status and code."""
    if isinstance(exc, (RegistryError, AgentError)):
        return HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": str(exc)}
        )
    return HTTPException(status_code=500, detail={"code": "internal_error", "message": str(exc)})
