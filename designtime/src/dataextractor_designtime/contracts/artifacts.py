"""The four design-time artifact kinds (CTR-06 .. CTR-11) and the package manifest (CTR-02).

Every artifact carries ``schema_version``; the loader resolves it against the
engine's supported set and never coerces an unsupported one (CTR-06).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from ..records import Confidence, Record


SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = {"1.0"}


def canonical_json(payload: Any) -> bytes:
    """Stable byte representation: sorted keys, no incidental whitespace.

    Checksums (CTR-02) and report hashes (DT-33) are taken over this form, so
    two semantically identical artifacts always hash the same.
    """
    if isinstance(payload, Record):
        payload = payload.to_dict()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class ArtifactKind(str, Enum):
    FIELD_SCHEMA = "field_schema"
    DETECTION_RULES = "detection_rules"
    SKILL_MANIFEST = "skill_manifest"
    THRESHOLDS = "thresholds"
    EVAL_REPORT = "eval_report"
    SKILL_BODY = "skill_body"


@dataclass(kw_only=True)
class _Artifact(Record):
    """Common provenance every artifact carries (CTR-11)."""

    schema_version: str = SCHEMA_VERSION
    generated_by: str
    reviewed_by: str | None = None

    def check(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"schema_unsupported: {self.schema_version}")

    @property
    def unreviewed(self) -> bool:
        """CTR-11 / DT-38: an unreviewed artifact cannot be promoted."""
        return not self.reviewed_by


# --------------------------------------------------------------------------
# Field schema (CTR-07)
# --------------------------------------------------------------------------

FieldType = Literal["string", "decimal", "date", "integer", "boolean"]


@dataclass(kw_only=True)
class FieldDef(Record):
    name: str
    type: FieldType
    critical: bool = False
    validation: dict[str, Any] = field(default_factory=dict)
    # DT-08: observed share of this type's samples the field appeared in.
    support: Confidence | None = None


@dataclass(kw_only=True)
class FieldSchema(_Artifact):
    email_type: str
    required_fields: list[FieldDef] = field(default_factory=list)
    optional_fields: list[FieldDef] = field(default_factory=list)
    # DT-09: label variants observed per canonical field. Labels only, never values.
    aliases: dict[str, list[str]] = field(default_factory=dict)

    def critical_field_names(self) -> list[str]:
        return [f.name for f in self.required_fields if f.critical]

    def required_field_names(self) -> list[str]:
        return [f.name for f in self.required_fields]


# --------------------------------------------------------------------------
# Detection rules (CTR-08)
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class Keyword(Record):
    term: str
    weight: Confidence
    support: int = 0


@dataclass(kw_only=True)
class Pattern(Record):
    name: str
    regex: str
    weight: Confidence
    support: int = 0


@dataclass(kw_only=True)
class TypeDetectionRules(Record):
    keywords: list[Keyword] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)
    entity_weights: dict[str, Confidence] = field(default_factory=dict)
    classification_threshold: Confidence = 0.6
    negative_signals: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class DetectionRules(_Artifact):
    types: dict[str, TypeDetectionRules] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Skill manifest (CTR-09)
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class SkillManifest(_Artifact):
    skill_id: str
    version: str = "1.0.0"
    body: str
    bound_agents: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    token_budget: int = 4000
    timeout_ms: int = 20000


# --------------------------------------------------------------------------
# Thresholds (CTR-10)
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class TypeThresholds(Record):
    accept_at: Confidence = 0.85
    review_below: Confidence = 0.85
    reject_below: Confidence = 0.40
    required_field_coverage: Confidence = 1.0
    critical_field_tolerance: dict[str, dict[str, float]] = field(default_factory=dict)
    always_review_if: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Thresholds(_Artifact):
    types: dict[str, TypeThresholds] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Package manifest (CTR-02 .. CTR-05)
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class ManifestArtifactEntry(Record):
    path: str
    kind: ArtifactKind
    sha256: str


@dataclass(kw_only=True)
class PackageManifest(Record):
    client_id: str
    workflow_id: str
    version: str
    engine_range: str
    email_types: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str
    source_corpus_id: str
    artifacts: list[ManifestArtifactEntry] = field(default_factory=list)
    eval: dict[str, Any] = field(default_factory=dict)

    @property
    def coordinate(self) -> str:
        return f"{self.client_id}/{self.workflow_id}@{self.version}"
