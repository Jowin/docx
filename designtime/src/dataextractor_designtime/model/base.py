"""The seam between an agent's mechanical work and its judgment step.

Everything an agent does deterministically — counting, parsing, grouping,
hashing, scoring against ground truth — lives in the agent. Everything that
needs judgment goes through a ModelClient. That boundary is what makes DT-35
(identical inputs reproduce identical metrics) checkable: swap in the stub and
the whole pipeline is deterministic; swap in a real client and only the
judgment steps move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelRequest:
    #: Names the judgment being asked for, e.g. "field_schema.propose_aliases".
    task: str
    #: Structured evidence the agent gathered. Never raw client content beyond
    #: what the task needs (CTR-28).
    evidence: dict[str, Any] = field(default_factory=dict)
    token_budget: int = 4000
    timeout_ms: int = 20000


@dataclass(frozen=True)
class ModelResponse:
    task: str
    output: dict[str, Any]
    #: Identifies what produced this, and lands in ``generated_by`` (CTR-11).
    produced_by: str
    deterministic: bool = False


class ModelClient(Protocol):
    """Implemented by StubModelClient today; by a real client later."""

    name: str

    def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover - protocol
        ...
