"""Postgres tables behind the registry.

The registry is the record *about* the runtime's config folders, which are
the source of truth (configroot.py): where each version came from, what it
scored, who signed it off, every release; and the log of design runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

#: The registry is Postgres only: JSONB documents and native UUID keys.
JSONType = JSONB()
UUIDType = PGUUID(as_uuid=False)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConfigVersionRow(Base):
    """The record of one config version folder: where it came from and what it scored.

    The folder (runtime config root) is the source of truth; this row is the
    record about it. ``sha256`` is the folder hash when it was published; a
    release is refused when the folder no longer matches.
    """

    __tablename__ = "config_versions"
    __table_args__ = (
        UniqueConstraint("client_id", "usecase", "version", name="uq_config_version"),
        Index("ix_config_versions_usecase", "client_id", "usecase"),
    )

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    usecase: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: authoring | learning | manual
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The design run (authoring or learning) that produced it.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    files: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Evaluation summary: metrics, gates, gate results (authoring), the judge's verdict (learning).
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: False when an evaluation gate failed; a release then needs an explicit override.
    gates_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    signoffs: Mapped[list["ConfigSignoffRow"]] = relationship(
        back_populates="config_version", cascade="all, delete-orphan", order_by="ConfigSignoffRow.created_at")


class ConfigSignoffRow(Base):
    """CTR-19: a named person accepted a config version for release."""

    __tablename__ = "config_signoffs"
    __table_args__ = (UniqueConstraint("config_version_id", "identity", name="uq_config_signoff"),)

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    config_version_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("config_versions.id", ondelete="CASCADE"), nullable=False)
    identity: Mapped[str] = mapped_column(String(256), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    config_version: Mapped[ConfigVersionRow] = relationship(back_populates="signoffs")


class ConfigReleaseRow(Base):
    """Every release, rollback and rejection, mirrored from releases.json with who and why."""

    __tablename__ = "config_releases"
    __table_args__ = (Index("ix_config_releases_usecase", "client_id", "usecase"),)

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    usecase: Mapped[str] = mapped_column(String(128), nullable=False)
    #: release | rollback | reject
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    previous: Mapped[str | None] = mapped_column(String(32), nullable=True)
    by: Mapped[str] = mapped_column(String(256), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The release went ahead although an evaluation gate had failed.
    gate_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class LearningRunRow(Base):
    """One design run: a pattern-learning call (kind ``pattern``) or an authoring run (``authoring``).

    Both kinds write config versions through the same publisher, so they share
    one log. For an authoring run ``pattern_name`` is "authoring", ``source`` the
    corpus id, ``attempts`` the stage records and ``verdict_after`` the final
    evaluation. Append-only. Pattern rows whose outcome left the source passing
    double as the regression set for later learning on the same use case.
    """

    __tablename__ = "learning_runs"
    __table_args__ = (
        Index("ix_learning_runs_pattern", "client_id", "usecase", "pattern_name"),
    )

    id: Mapped[str] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    #: pattern | authoring
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="pattern", server_default="pattern")
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
    #: The graph nodes the run went through, in order.
    path: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(256), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
