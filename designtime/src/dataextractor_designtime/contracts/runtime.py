"""Runtime output contracts (CTR-12 .. CTR-16).

Design-time does not produce these, but the evaluation harness replays the
runtime engine and validates what comes back, so the models live here and are
imported by both sides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..records import Confidence, Record


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


@dataclass(kw_only=True)
class ExtractedValue(Record):
    """CTR-12: every extracted value is {value, source, confidence}."""

    value: Any
    source: str
    confidence: Confidence

    def check(self) -> None:
        if not _SOURCE_RE.match(self.source):
            raise ValueError(f"source not locatable (CTR-13): {self.source!r}")


@dataclass(kw_only=True)
class ExtractionResult(Record):
    audit_id: str
    client_id: str
    workflow_id: str
    package_version: str
    engine_version: str
    type: str
    confidence: Confidence
    fields: dict[str, ExtractedValue] = field(default_factory=dict)
    human_corrected: bool = False
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(kw_only=True)
class ReviewQueueEntry(Record):
    audit_id: str
    client_id: str
    workflow_id: str
    package_version: str
    type: str | None = None
    confidence: Confidence = 0.0
    partial_fields: dict[str, ExtractedValue] = field(default_factory=dict)
    review_reason: list[str] = field(default_factory=list)
    raw_sources: list[str] = field(default_factory=list)
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def check(self) -> None:
        self.review_reason = [validate_reason_code(code) for code in self.review_reason]


@dataclass(kw_only=True)
class AgentTraceEntry(Record):
    #: Agents add their own trace keys; they are kept, flattened, in ``to_dict()``.
    __extra__ = "allow"

    agent: str
    started_ms: int = 0
    duration_ms: int = 0
    status: Literal["ok", "flagged", "failed", "skipped"] = "ok"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class AuditRecord(Record):
    """CTR-14: one per run, always, including on hard failure."""

    audit_id: str
    package_version: str
    engine_version: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    outcome: Literal["extracted", "review_queued", "error"]
    agent_trace: list[AgentTraceEntry] = field(default_factory=list)
    field_attribution: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)
