"""Typed service errors. Every failure the API returns carries a stable code."""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """An error the API turns into a JSON response with this HTTP status."""

    def __init__(self, status: int, code: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "detail": self.detail}


class ConfigError(ServiceError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None,
                 status: int = 422) -> None:
        super().__init__(status, code, message, detail)


class ModelError(Exception):
    """A model call failed. ``transient`` says whether a retry could help."""

    def __init__(self, code: str, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.transient = transient
