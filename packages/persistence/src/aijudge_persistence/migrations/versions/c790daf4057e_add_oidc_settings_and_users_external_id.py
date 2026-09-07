"""add oidc settings and users external id

Revision ID: c790daf4057e
Revises: a69f2885e5c2
Create Date: 2026-09-07 11:57:33.010922
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "c790daf4057e"
down_revision: str | None = "a69f2885e5c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite は列追加後の制約追加を ALTER TABLE で受け付けない
    # （batch モード＝コピーして作り直す方式でしか通らない）。
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("external_id", sa.String(length=256), nullable=True))
        batch_op.create_unique_constraint(
            "uq_users_tenant_external_id", ["tenant_id", "external_id"]
        )
    op.create_table(
        "oidc_settings",
        sa.Column("tenant_id", sa.String(length=64), primary_key=True),
        sa.Column("client_id", sa.String(length=256), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "allowed_domains",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oidc_settings")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_tenant_external_id", type_="unique")
        batch_op.drop_column("external_id")
