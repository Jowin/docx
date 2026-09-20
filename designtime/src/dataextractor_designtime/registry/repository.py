"""The artifact registry: publish, sign off, promote, activate, roll back.

Backed by Postgres. Every rule enforced here comes from PRD 1 and is named in
the raised error, so a caller never has to guess which contract it broke.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..contracts.artifacts import ArtifactKind, PackageManifest, sha256_of
from . import semver
from .errors import (
    EngineIncompatible,
    GateFailed,
    PackageCorrupt,
    PackageNotFound,
    SignoffRequired,
    TypeIncomplete,
    VersionBumpInsufficient,
    VersionExists,
)
from .models import (
    ActivationRow,
    ArtifactRow,
    PackageRow,
    PackageState,
    PromotionRow,
    SignoffRow,
)

#: CTR-25: total package size ceiling.
MAX_PACKAGE_BYTES = 10 * 1024 * 1024

#: CTR-11 applies to the artifacts a human accepts. An evaluation report is
#: evidence produced by the harness, and a skill body is signed off through its
#: manifest, so neither carries its own reviewer.
REVIEWABLE_KINDS = frozenset(
    {
        ArtifactKind.FIELD_SCHEMA.value,
        ArtifactKind.DETECTION_RULES.value,
        ArtifactKind.SKILL_MANIFEST.value,
        ArtifactKind.THRESHOLDS.value,
    }
)


class Registry:
    """Session-scoped facade over the registry tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reads -------------------------------------------------------------

    def get(
        self,
        client_id: str,
        workflow_id: str,
        version: str,
        *,
        verify: bool = True,
    ) -> PackageRow:
        stmt = select(PackageRow).where(
            PackageRow.client_id == client_id,
            PackageRow.workflow_id == workflow_id,
            PackageRow.version == version,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise PackageNotFound(f"{client_id}/{workflow_id}@{version}")
        if verify:
            self.verify_checksums(row)
        return row

    def list_packages(
        self, client_id: str | None = None, workflow_id: str | None = None
    ) -> Sequence[PackageRow]:
        stmt = select(PackageRow)
        if client_id:
            stmt = stmt.where(PackageRow.client_id == client_id)
        if workflow_id:
            stmt = stmt.where(PackageRow.workflow_id == workflow_id)
        rows = list(self.session.execute(stmt).scalars())
        rows.sort(key=lambda r: (r.client_id, r.workflow_id, semver.parse(r.version)))
        return rows

    def latest(self, client_id: str, workflow_id: str) -> PackageRow | None:
        rows = self.list_packages(client_id, workflow_id)
        return rows[-1] if rows else None

    def artifact_contents(self, pkg: PackageRow) -> dict[str, Any]:
        """path -> parsed content (or body text for a skill body)."""
        out: dict[str, Any] = {}
        for art in pkg.artifacts:
            out[art.path] = art.content if art.content is not None else art.body
        return out

    def active(self, client_id: str, workflow_id: str) -> PackageRow | None:
        row = self._activation(client_id, workflow_id, create=False)
        if row is None or row.active_package_id is None:
            return None
        return self.session.get(PackageRow, row.active_package_id)

    def resolve_for_engine(
        self, client_id: str, workflow_id: str, engine_version: str
    ) -> PackageRow:
        """What runtime does at run start (RT-01): active version, engine-checked."""
        pkg = self.active(client_id, workflow_id)
        if pkg is None:
            raise PackageNotFound(f"no active package for {client_id}/{workflow_id}")
        if not semver.range_matches(pkg.engine_range, engine_version):
            raise EngineIncompatible(
                f"engine {engine_version} outside {pkg.engine_range} for {pkg.coordinate}"
            )
        self.verify_checksums(pkg)
        return pkg

    # -- integrity ---------------------------------------------------------

    def verify_checksums(self, pkg: PackageRow) -> None:
        """CTR-02: recompute every artifact checksum; any mismatch fails the load."""
        listed = {entry["path"]: entry["sha256"] for entry in pkg.manifest.get("artifacts", [])}
        stored = {art.path: art for art in pkg.artifacts}

        missing = set(listed) - set(stored)
        if missing:
            raise PackageCorrupt(f"{pkg.coordinate}: manifest lists absent artifacts {sorted(missing)}")
        extra = set(stored) - set(listed)
        if extra:
            raise PackageCorrupt(f"{pkg.coordinate}: unlisted artifacts present {sorted(extra)}")

        for path, expected in listed.items():
            art = stored[path]
            payload = art.content if art.content is not None else art.body
            actual = sha256_of(payload)
            if actual != expected or actual != art.sha256:
                raise PackageCorrupt(f"{pkg.coordinate}: checksum mismatch on {path}")

    def verify_promotion(self, pkg: PackageRow) -> bool:
        """CTR-18: the promoted bytes still match what passed evaluation."""
        promotions = [p for p in pkg.promotions if p.action == "promote"]
        if not promotions:
            return False
        latest = max(promotions, key=lambda p: p.promoted_at)
        self.verify_checksums(pkg)
        return latest.manifest_sha256 == sha256_of(pkg.manifest)

    # -- writes ------------------------------------------------------------

    def publish(
        self,
        *,
        manifest: PackageManifest,
        artifacts: dict[str, Any],
        bodies: dict[str, str] | None = None,
        eval_report: dict[str, Any] | None = None,
        gate_failed: bool = False,
    ) -> PackageRow:
        """Publish an immutable package version to the UAT channel.

        ``artifacts`` maps path -> parsed JSON content; ``bodies`` maps path ->
        text for non-JSON artifacts such as skill bodies.
        """
        bodies = bodies or {}
        existing = self.session.execute(
            select(PackageRow).where(
                PackageRow.client_id == manifest.client_id,
                PackageRow.workflow_id == manifest.workflow_id,
                PackageRow.version == manifest.version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise VersionExists(
                f"{manifest.coordinate} already published; versions are immutable (CTR-01)"
            )

        self._validate_self_contained(list(artifacts) + list(bodies))
        self._validate_manifest_checksums(manifest, artifacts, bodies)
        self._validate_types_complete(manifest, artifacts)
        self._validate_size(artifacts, bodies)
        self._validate_bump(manifest, artifacts)

        pkg = PackageRow(
            client_id=manifest.client_id,
            workflow_id=manifest.workflow_id,
            version=manifest.version,
            engine_range=manifest.engine_range,
            email_types=list(manifest.email_types),
            source_corpus_id=manifest.source_corpus_id,
            created_by=manifest.created_by,
            manifest=manifest.model_dump(mode="json"),
            manifest_sha256=sha256_of(manifest),
            eval_report=eval_report,
            eval_report_sha256=sha256_of(eval_report) if eval_report is not None else None,
            gate_failed=gate_failed,
            state=PackageState.PUBLISHED,
        )

        by_path = {entry.path: entry for entry in manifest.artifacts}
        for path, content in artifacts.items():
            entry = by_path[path]
            pkg.artifacts.append(
                ArtifactRow(
                    path=path,
                    kind=entry.kind.value,
                    sha256=entry.sha256,
                    content=content,
                    generated_by=str(content.get("generated_by", manifest.created_by))
                    if isinstance(content, dict)
                    else manifest.created_by,
                    reviewed_by=content.get("reviewed_by") if isinstance(content, dict) else None,
                )
            )
        for path, text in bodies.items():
            entry = by_path[path]
            pkg.artifacts.append(
                ArtifactRow(
                    path=path,
                    kind=entry.kind.value,
                    sha256=entry.sha256,
                    body=text,
                    generated_by=manifest.created_by,
                )
            )

        self.session.add(pkg)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise VersionExists(
                f"{manifest.coordinate} already published; versions are immutable (CTR-01)"
            ) from exc
        # Ensure a workflow always has an activation row to point at later.
        self._activation(manifest.client_id, manifest.workflow_id, create=True)
        return pkg

    def sign_off(self, pkg: PackageRow, identity: str, note: str | None = None) -> SignoffRow:
        """DT-33: immutable record of who accepted which numbers."""
        if pkg.eval_report_sha256 is None:
            raise SignoffRequired(f"{pkg.coordinate}: no evaluation report to sign off against")
        unreviewed = [
            a.path
            for a in pkg.artifacts
            if a.kind in REVIEWABLE_KINDS and not a.reviewed_by
        ]
        if unreviewed:
            raise SignoffRequired(
                f"{pkg.coordinate}: unreviewed artifacts block sign-off (CTR-11): {sorted(unreviewed)}"
            )
        row = SignoffRow(
            identity=identity,
            eval_report_sha256=pkg.eval_report_sha256,
            note=note,
        )
        pkg.signoffs.append(row)
        self.session.flush()
        return row

    def promote(self, pkg: PackageRow, promoted_by: str) -> PromotionRow:
        """CTR-19: passing report plus recorded sign-off, or nothing moves."""
        if pkg.gate_failed:
            raise GateFailed(f"{pkg.coordinate}: package failed an evaluation gate (DT-34)")
        if pkg.eval_report_sha256 is None:
            raise SignoffRequired(f"{pkg.coordinate}: no evaluation report")
        matching = [s for s in pkg.signoffs if s.eval_report_sha256 == pkg.eval_report_sha256]
        if not matching:
            raise SignoffRequired(
                f"{pkg.coordinate}: no sign-off against the current evaluation report"
            )
        self.verify_checksums(pkg)

        promotion = PromotionRow(
            promoted_by=promoted_by,
            eval_report_sha256=pkg.eval_report_sha256,
            manifest_sha256=sha256_of(pkg.manifest),
            action="promote",
        )
        pkg.promotions.append(promotion)

        activation = self._activation(pkg.client_id, pkg.workflow_id, create=True)
        previous_id = activation.active_package_id
        if previous_id and previous_id != pkg.id:
            previous = self.session.get(PackageRow, previous_id)
            if previous is not None:
                previous.state = PackageState.DEPRECATED
            activation.previous_package_id = previous_id
        activation.active_package_id = pkg.id
        pkg.state = PackageState.PROMOTED
        self.session.flush()
        return promotion

    def rollback(self, client_id: str, workflow_id: str, rolled_back_by: str) -> PromotionRow:
        """CTR-20: a pointer change back to the previously promoted version."""
        activation = self._activation(client_id, workflow_id, create=False)
        if activation is None or activation.previous_package_id is None:
            raise PackageNotFound(f"no previous version to roll back to for {client_id}/{workflow_id}")

        current = (
            self.session.get(PackageRow, activation.active_package_id)
            if activation.active_package_id
            else None
        )
        target = self.session.get(PackageRow, activation.previous_package_id)
        if target is None:
            raise PackageNotFound("previous package row missing")

        if current is not None:
            current.state = PackageState.ROLLED_BACK
        target.state = PackageState.PROMOTED
        activation.active_package_id = target.id
        activation.previous_package_id = current.id if current is not None else None

        promotion = PromotionRow(
            promoted_by=rolled_back_by,
            eval_report_sha256=target.eval_report_sha256 or "",
            manifest_sha256=sha256_of(target.manifest),
            action="rollback",
        )
        target.promotions.append(promotion)
        self.session.flush()
        return promotion

    def withdraw(self, pkg: PackageRow, reason: str = "") -> PackageRow:
        pkg.state = PackageState.WITHDRAWN
        self.session.flush()
        return pkg

    # -- internals ---------------------------------------------------------

    def _activation(
        self, client_id: str, workflow_id: str, *, create: bool
    ) -> ActivationRow | None:
        stmt = select(ActivationRow).where(
            ActivationRow.client_id == client_id,
            ActivationRow.workflow_id == workflow_id,
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None and create:
            row = ActivationRow(client_id=client_id, workflow_id=workflow_id)
            self.session.add(row)
            self.session.flush()
        return row

    @staticmethod
    def _validate_self_contained(paths: Iterable[str]) -> None:
        """CTR-04: no artifact may reference a file outside its own package."""
        for path in paths:
            if path.startswith("/") or path.startswith("\\") or ".." in path.split("/"):
                raise PackageCorrupt(f"artifact path escapes the package (CTR-04): {path!r}")
            if ":" in path:  # drive letters and URL schemes
                raise PackageCorrupt(f"artifact path is not package-relative (CTR-04): {path!r}")

    @staticmethod
    def _validate_types_complete(manifest: PackageManifest, artifacts: dict[str, Any]) -> None:
        """CTR-05: one field schema and one threshold entry per declared type."""
        schema_types = {
            content.get("email_type")
            for path, content in artifacts.items()
            if isinstance(content, dict) and path.startswith("schemas/")
        }
        threshold_types: set[str] = set()
        for path, content in artifacts.items():
            if isinstance(content, dict) and "types" in content and path.endswith("thresholds.json"):
                threshold_types |= set(content["types"])

        for email_type in manifest.email_types:
            if email_type not in schema_types:
                raise TypeIncomplete(f"{email_type}: no field schema in package")
            if email_type not in threshold_types:
                raise TypeIncomplete(f"{email_type}: no thresholds entry in package")

    @staticmethod
    def _validate_manifest_checksums(
        manifest: PackageManifest, artifacts: dict[str, Any], bodies: dict[str, str]
    ) -> None:
        """CTR-02: the manifest lists every file, with a checksum that matches."""
        listed = {entry.path: entry.sha256 for entry in manifest.artifacts}
        supplied = set(artifacts) | set(bodies)
        if set(listed) != supplied:
            raise PackageCorrupt(
                "manifest and supplied artifacts disagree: "
                f"listed-not-supplied={sorted(set(listed) - supplied)}, "
                f"supplied-not-listed={sorted(supplied - set(listed))}"
            )
        for path, expected in listed.items():
            payload = artifacts.get(path, bodies.get(path))
            if sha256_of(payload) != expected:
                raise PackageCorrupt(f"manifest checksum does not match content for {path}")

    @staticmethod
    def _validate_size(artifacts: dict[str, Any], bodies: dict[str, str]) -> None:
        total = sum(len(json.dumps(c).encode()) for c in artifacts.values())
        total += sum(len(b.encode()) for b in bodies.values())
        if total > MAX_PACKAGE_BYTES:
            raise PackageCorrupt(f"package is {total} bytes, over the {MAX_PACKAGE_BYTES} ceiling (CTR-25)")

    def _validate_bump(self, manifest: PackageManifest, artifacts: dict[str, Any]) -> None:
        """CTR-17: refuse a bump that understates the change."""
        previous = self.latest(manifest.client_id, manifest.workflow_id)
        if previous is None:
            return
        prev_contents = {
            path: content
            for path, content in self.artifact_contents(previous).items()
            if isinstance(content, dict)
        }
        new_contents = {p: c for p, c in artifacts.items() if isinstance(c, dict)}
        required = semver.required_bump(prev_contents, new_contents)
        declared = semver.observed_bump(previous.version, manifest.version)
        if not semver.is_sufficient(declared, required):
            raise VersionBumpInsufficient(
                f"{manifest.coordinate}: diff requires a {required} bump, "
                f"{previous.version} -> {manifest.version} is only {declared}"
            )
