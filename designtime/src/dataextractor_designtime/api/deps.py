"""Shared FastAPI dependencies and error translation."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..agents.base import AgentError
from ..config import Settings, get_settings
from ..model.base import ModelClient
from ..model.gateway_client import default_client
from ..registry.db import get_sessionmaker
from ..registry.errors import RegistryError
from ..records import ValidationError
from ..registry.configs import ConfigRegistry


def settings_dep() -> Settings:
    return get_settings()


def model_dep(request: Request) -> ModelClient:
    """Lets a test or a deployment swap the judgment backend in one place.

    Without an override: the gateway client when MODEL_GATEWAY_URL is set
    (it answers the tasks it has prompts for and leaves the rest to the
    stub), otherwise the deterministic stub.
    """
    return getattr(request.app.state, "model_client", None) or default_client(get_settings())


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


def registry_dep(session: Session = Depends(session_dep)) -> ConfigRegistry:
    """The registry of config versions, over the runtime config root it records."""
    return ConfigRegistry(session, get_settings().runtime_config_root)


def as_http(exc: Exception) -> HTTPException:
    """Registry and agent errors carry their own status and code."""
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail={"code": "invalid_input", "message": str(exc),
                                                      "errors": exc.errors})
    if isinstance(exc, (RegistryError, AgentError)):
        detail = {"code": exc.code, "message": str(exc)}
        if getattr(exc, "detail", None):
            detail["detail"] = exc.detail
        return HTTPException(status_code=exc.status, detail=detail)
    return HTTPException(status_code=500, detail={"code": "internal_error", "message": str(exc)})
