"""Shared shape for every design agent.

An agent owns one stage and at most one artifact kind. It takes a typed input,
returns a typed output, and raises nothing but ``AgentError`` for conditions the
caller is expected to handle. Anything needing judgment goes through the
ModelClient seam rather than being decided in the agent itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

from ..records import Record
from ..model.base import ModelClient

InputT = TypeVar("InputT", bound=Record)
OutputT = TypeVar("OutputT", bound=Record)


class AgentError(Exception):
    """A condition the caller should surface, not a bug."""

    code = "agent_error"
    status = 422

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status
        self.detail = detail


class CorpusTooSmall(AgentError):
    """DT-18: intake floor not met, per type."""

    code = "corpus_too_small"
    status = 422


class ConfirmationRequired(AgentError):
    """DT-07: generation cannot start before the type list is confirmed."""

    code = "confirmation_required"
    status = 409


class DesignAgent(Generic[InputT, OutputT]):
    """Base class. Subclasses set ``name``/``version`` and implement ``run``."""

    name: ClassVar[str] = "agent"
    version: ClassVar[str] = "0.1.0"

    def __init__(self, model: ModelClient | None = None) -> None:
        from ..model.stub import StubModelClient

        self.model: ModelClient = model or StubModelClient()

    @property
    def identity(self) -> str:
        """Lands in ``generated_by`` on every artifact this agent produces (DT-49, CTR-11)."""
        return f"design-agent:{self.name}@{self.version}"

    def run(self, payload: InputT) -> OutputT:  # pragma: no cover - abstract
        raise NotImplementedError
