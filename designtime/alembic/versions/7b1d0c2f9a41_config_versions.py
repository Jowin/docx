"""config versions replace packages; design runs get a kind and a path

The runtime's config folders are the source of truth. The package tables
(packages, artifacts, signoffs, promotions, activations) recorded a second
format that the runtime never served; they are replaced by the record of
config versions, their sign-offs and their releases.

Revision ID: 7b1d0c2f9a41
Revises: e6c3b2445387
Create Date: 2026-10-04 09:00:00
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7b1d0c2f9a41"
down_revision: str | None = "e6c3b2445387"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = sa.UUID(as_uuid=False)


def upgrade() -> None:
    op.drop_table("signoffs")
    op.drop_table("promotions")
    op.drop_table("artifacts")
    op.drop_table("activations")
    op.drop_index("ix_packages_workflow", table_name="packages")
    op.drop_table("packages")

    op.create_table(
        "config_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("usecase", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("base_version", sa.String(32), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("evaluation", postgresql.JSONB(), nullable=True),
        sa.Column("gates_passed", sa.Boolean(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "usecase", "version", name="uq_config_version"),
    )
    op.create_index("ix_config_versions_usecase", "config_versions", ["client_id", "usecase"])
    op.create_table(
        "config_signoffs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("config_version_id", UUID, nullable=False),
        sa.Column("identity", sa.String(256), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["config_version_id"], ["config_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("config_version_id", "identity", name="uq_config_signoff"),
    )
    op.create_table(
        "config_releases",
        sa.Column("id", UUID, nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("usecase", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("previous", sa.String(32), nullable=True),
        sa.Column("by", sa.String(256), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("gate_override", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_config_releases_usecase", "config_releases", ["client_id", "usecase"])
    op.add_column("learning_runs", sa.Column("kind", sa.String(16), nullable=False, server_default="pattern"))
    op.add_column("learning_runs", sa.Column("path", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("the package tables are retired; restore from a backup taken before upgrading")
