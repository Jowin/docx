"""Postgres tables behind the artifact registry.

The registry is the single store of workflow packages (PRD 1). Design-time
writes the UAT channel; promotion marks a package for production; runtime reads
production only (RT-05).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

#: JSONB on Postgres, plain JSON elsewhere so the suite can also run on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")
UUIDType = String(36).with_variant(PGUUID(as_uuid=False), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PackageState(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"      # in the UAT channel
    PROMOTED = "promoted"        # copied to production
    DEPRECATED = "deprecated"    # superseded by a newer promoted version
    WITHDRAWN = "withdrawn"      # defect found before promotion
    ROLLED_BACK = "rolled_back"  # promoted, then rolled back after an incident


class PackageRow(Base):
    """One immutable workflow package version (CTR-01)."""

    __tablename__ = "packages"
    __table_args__ = (
        UniqueConstraint("client_id", "workflow_id", "version", name="uq_package_coordinate"),
        Index("ix_packages_workflow", "client_id", "workflow_id"),
    )

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    engine_range: Mapped[str] = mapped_column(String(64), nullable=False)
    email_types: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    source_corpus_id: Mapped[str] = mapped_column(String(256), nullable=False)
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    #: The manifest exactly as published. Checksums are recomputed against it
    #: on every load (CTR-02).
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    eval_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    eval_report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: DT-34: publishable to UAT, but blocked from promotion.
    gate_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    state: Mapped[PackageState] = mapped_column(
        SAEnum(PackageState, name="package_state", native_enum=False, length=16),
        default=PackageState.PUBLISHED,
        nullable=False,
    )

    artifacts: Mapped[list["ArtifactRow"]] = relationship(
        back_populates="package", cascade="all, delete-orphan", lazy="selectin"
    )
    signoffs: Mapped[list["SignoffRow"]] = relationship(
        back_populates="package", cascade="all, delete-orphan", lazy="selectin"
    )
    promotions: Mapped[list["PromotionRow"]] = relationship(
        back_populates="package", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def coordinate(self) -> str:
        return f"{self.client_id}/{self.workflow_id}@{self.version}"


class ArtifactRow(Base):
    """One artifact file inside a package, with its published checksum."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("package_id", "path", name="uq_artifact_path"),)

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    package_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: JSON artifacts hold their parsed content; a skill body holds its text.
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    package: Mapped[PackageRow] = relationship(back_populates="artifacts")


class SignoffRow(Base):
    """Immutable human sign-off against a specific eval report (DT-33)."""

    __tablename__ = "signoffs"

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    package_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    identity: Mapped[str] = mapped_column(String(256), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    eval_report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    package: Mapped[PackageRow] = relationship(back_populates="signoffs")


class PromotionRow(Base):
    """Who promoted what, when, against which report (CTR-21). Append-only."""

    __tablename__ = "promotions"

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    package_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("packages.id", ondelete="CASCADE"), nullable=False
    )
    promoted_by: Mapped[str] = mapped_column(String(256), nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    eval_report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Recorded at promotion so CTR-18 can be re-verified at any later time.
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), default="promote", nullable=False)

    package: Mapped[PackageRow] = relationship(back_populates="promotions")


class ActivationRow(Base):
    """CTR-20: exactly one active version per workflow, previous kept for rollback."""

    __tablename__ = "activations"
    __table_args__ = (UniqueConstraint("client_id", "workflow_id", name="uq_activation_workflow"),)

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    active_package_id: Mapped[str | None] = mapped_column(
        UUIDType, ForeignKey("packages.id", ondelete="RESTRICT"), nullable=True
    )
    previous_package_id: Mapped[str | None] = mapped_column(
        UUIDType, ForeignKey("packages.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class LearningRunRow(Base):
    """One call of the pattern-learning loop: what was tried, what it scored, what it wrote.

    Append-only. Rows whose outcome left the source passing double as the
    regression set for later learning on the same client and use case.
    """

    __tablename__ = "learning_runs"
    __table_args__ = (
        Index("ix_learning_runs_pattern", "client_id", "usecase", "pattern_name"),
    )

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    usecase: Mapped[str] = mapped_column(String(128), nullable=False)
    object_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pattern_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ground_truth: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    reference_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The config version this run wrote, if it wrote one.
    result_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: passed | learned | improved | failed | error
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    passed_before: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passed_after: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verdict_before: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    verdict_after: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    skill_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    skill_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(256), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
