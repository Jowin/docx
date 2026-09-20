"""Runtime output contracts (CTR-12 .. CTR-16).

Design-time does not produce these, but the evaluation harness replays the
runtime engine and validates what comes back, so the models live here and are
imported by both sides.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

# CTR-16: closed vocabulary. Codes with an argument use "<code>:<arg>".
REVIEW_REASON_SIMPLE = frozenset(
    {
        "low_confidence",
        "encrypted_no_key",
        "embedded_depth_exceeded",
        "classification_ambiguous",
    }
)
REVIEW_REASON_PREFIXES = frozenset(
    {
        "missing_field",
        "critical_field_conflict",
        "attachment_parse_failed",
        "unsupported_attachment",
        "agent_timeout",
        "agent_failed",
        "schema_validation_failed",
    }
)

# CTR-13: source must identify origin precisely enough to re-open it.
_SOURCE_RE = re.compile(
    r"^(body:segment_\d+|attachment:[^#]+(#.+)?|embedded:\d+:.+)$"
)


def validate_reason_code(code: str) -> str:
    if code in REVIEW_REASON_SIMPLE:
        return code
    head, sep, arg = code.partition(":")
    if sep and head in REVIEW_REASON_PREFIXES and arg:
        return code
    raise ValueError(f"unknown review_reason code: {code!r}")


class ExtractedValue(BaseModel):
    """CTR-12: every extracted value is {value, source, confidence}."""

    model_config = ConfigDict(extra="forbid")

    value: Any
    source: str
    confidence: Confidence

    @field_validator("source")
    @classmethod
    def _locatable(cls, v: str) -> str:
        if not _SOURCE_RE.match(v):
            raise ValueError(f"source not locatable (CTR-13): {v!r}")
        return v


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    client_id: str
    workflow_id: str
    package_version: str
    engine_version: str
    type: str
    confidence: Confidence
    fields: dict[str, ExtractedValue] = Field(default_factory=dict)
    human_corrected: bool = False
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    client_id: str
    workflow_id: str
    package_version: str
    type: str | None = None
    confidence: Confidence = 0.0
    partial_fields: dict[str, ExtractedValue] = Field(default_factory=dict)
    review_reason: list[str] = Field(default_factory=list)
    raw_sources: list[str] = Field(default_factory=list)
    queued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("review_reason")
    @classmethod
    def _closed_vocabulary(cls, v: list[str]) -> list[str]:
        return [validate_reason_code(code) for code in v]


class AgentTraceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent: str
    started_ms: int = 0
    duration_ms: int = 0
    status: Literal["ok", "flagged", "failed", "skipped"] = "ok"


class AuditRecord(BaseModel):
    """CTR-14: one per run, always, including on hard failure."""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    package_version: str
    engine_version: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    outcome: Literal["extracted", "review_queued", "error"]
    agent_trace: list[AgentTraceEntry] = Field(default_factory=list)
    field_attribution: list[dict[str, Any]] = Field(default_factory=list)
    retries: list[dict[str, Any]] = Field(default_factory=list)
