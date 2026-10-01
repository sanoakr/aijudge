"""blind marks can be corrected

blind 採点の訂正を追記する表（ADR 0031）。元の `blind_marks` は書き換えない ──
訂正は AI を見たあとに起きるので、元の値と並べて残さないと、測定に紛れた偏りを
後から確かめようがない。

Revision ID: d5b8e3a7c0f2
Revises: aa5e81406d1c
Create Date: 2026-10-01 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "d5b8e3a7c0f2"
down_revision: str | None = "aa5e81406d1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blind_mark_corrections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("submission_id", sa.String(length=64), nullable=False),
        sa.Column("corrected_by", sa.String(length=64), nullable=False),
        sa.Column(
            "corrected_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "document",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_blind_mark_corrections_submission_id"),
        "blind_mark_corrections",
        ["submission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_blind_mark_corrections_corrected_by"),
        "blind_mark_corrections",
        ["corrected_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_blind_mark_corrections_corrected_by"), table_name="blind_mark_corrections"
    )
    op.drop_index(
        op.f("ix_blind_mark_corrections_submission_id"), table_name="blind_mark_corrections"
    )
    op.drop_table("blind_mark_corrections")
