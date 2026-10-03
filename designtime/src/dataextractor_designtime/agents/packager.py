"""Packager (DT-15, DT-16, CTR-01 .. CTR-05).

Assembles accepted artifacts into a package: fixed paths, a manifest with a
checksum per file, provenance on every artifact, and a semver bump derived from
the diff against the previous version rather than chosen by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..records import Record
from ..config import get_settings
from ..contracts.artifacts import (
    ArtifactKind,
    DetectionRules,
    FieldSchema,
    ManifestArtifactEntry,
    PackageManifest,
    SkillManifest,
    Thresholds,
    sha256_of,
)
from ..registry import semver
from .base import AgentError, DesignAgent

THRESHOLDS_PATH = "thresholds.json"
DETECTION_PATH = "rules/detection.json"
EVAL_PATH = "eval/report.json"


@dataclass(kw_only=True)
class SkillBundle(Record):
    manifest: SkillManifest
    body: str


@dataclass(kw_only=True)
class PackagerInput(Record):
    client_id: str
    workflow_id: str
    source_corpus_id: str
    schemas: dict[str, FieldSchema]
    detection: DetectionRules
    thresholds: Thresholds
    skills: list[SkillBundle] = field(default_factory=list)
    eval_report: dict[str, Any] | None = None
    #: The version the designer intends to publish. Omit to let the packager
    #: derive the smallest sufficient bump from the diff.
    version: str | None = None
    previous_version: str | None = None
    previous_artifacts: dict[str, Any] | None = None
    engine_range: str | None = None
    reviewed_by: str | None = None
    email_types: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class PackagerOutput(Record):
    manifest: PackageManifest
    artifacts: dict[str, Any] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    required_bump: str
    declared_bump: str
    #: DT-34: set when the supplied evaluation report failed a gate.
    gate_failed: bool = False


class Packager(DesignAgent[PackagerInput, PackagerOutput]):
    name = "packager"

    def run(self, payload: PackagerInput) -> PackagerOutput:
        settings = get_settings()
        artifacts: dict[str, Any] = {}
        bodies: dict[str, str] = {}
        entries: list[ManifestArtifactEntry] = []

        def add_json(path: str, model: Any, kind: ArtifactKind) -> None:
            if payload.reviewed_by and getattr(model, "reviewed_by", None) is None:
                model = model.replace(reviewed_by=payload.reviewed_by)
            content = model.to_dict() if isinstance(model, Record) else model
            artifacts[path] = content
            entries.append(ManifestArtifactEntry(path=path, kind=kind, sha256=sha256_of(content)))

        email_types = payload.email_types or sorted(payload.schemas)
        for email_type in email_types:
            schema = payload.schemas.get(email_type)
            if schema is None:
                raise AgentError(
                    f"{email_type}: no field schema supplied (CTR-05)", code="type_incomplete"
                )
            add_json(f"schemas/{email_type}.json", schema, ArtifactKind.FIELD_SCHEMA)

        add_json(DETECTION_PATH, payload.detection, ArtifactKind.DETECTION_RULES)
        add_json(THRESHOLDS_PATH, payload.thresholds, ArtifactKind.THRESHOLDS)

        for bundle in payload.skills:
            add_json(
                f"skills/{bundle.manifest.skill_id}.json",
                bundle.manifest,
                ArtifactKind.SKILL_MANIFEST,
            )
            body_path = f"skills/{bundle.manifest.skill_id}.md"
            bodies[body_path] = bundle.body
            entries.append(
                ManifestArtifactEntry(
                    path=body_path, kind=ArtifactKind.SKILL_BODY, sha256=sha256_of(bundle.body)
                )
            )

        if payload.eval_report is not None:
            artifacts[EVAL_PATH] = payload.eval_report
            entries.append(
                ManifestArtifactEntry(
                    path=EVAL_PATH, kind=ArtifactKind.EVAL_REPORT, sha256=sha256_of(payload.eval_report)
                )
            )

        comparable = {p: c for p, c in artifacts.items() if p != EVAL_PATH and isinstance(c, dict)}
        previous = (
            {p: c for p, c in payload.previous_artifacts.items() if p != EVAL_PATH and isinstance(c, dict)}
            if payload.previous_artifacts is not None
            else None
        )
        required = semver.required_bump(previous, comparable)

        version = payload.version or self._next_version(payload.previous_version, required)
        declared = (
            semver.observed_bump(payload.previous_version, version)
            if payload.previous_version
            else "none"
        )
        if payload.previous_version and not semver.is_sufficient(declared, required):
            raise AgentError(
                f"diff requires a {required} bump; {payload.previous_version} -> {version} "
                f"is only {declared} (CTR-17)",
                code="version_bump_insufficient",
                status=409,
            )

        gate_failed = bool(
            payload.eval_report
            and not all(payload.eval_report.get("gate_results", {}).values())
        )

        manifest = PackageManifest(
            client_id=payload.client_id,
            workflow_id=payload.workflow_id,
            version=version,
            engine_range=payload.engine_range or settings.engine_range,
            email_types=list(email_types),
            created_by=self.identity,
            source_corpus_id=payload.source_corpus_id,
            artifacts=sorted(entries, key=lambda e: e.path),
            eval=(
                {
                    "report": EVAL_PATH,
                    "corpus_size": payload.eval_report.get("corpus_size"),
                    "field_accuracy": payload.eval_report.get("metrics", {}).get("field_accuracy"),
                }
                if payload.eval_report
                else {}
            ),
        )

        return PackagerOutput(
            manifest=manifest,
            artifacts=artifacts,
            bodies=bodies,
            required_bump=required,
            declared_bump=declared,
            gate_failed=gate_failed,
        )

    @staticmethod
    def _next_version(previous: str | None, required: str) -> str:
        if previous is None:
            return "1.0.0"
        major, minor, patch = semver.parse(previous)
        if required == "major":
            return f"{major + 1}.0.0"
        if required == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major}.{minor}.{patch + 1}"
