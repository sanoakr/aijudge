"""record where an IDE submission came from

IDE からの提出の出どころ（設計書 §9）。本人が押した提出と、受付終了時に
サーバが自動保存の最新を出した提出（§9.1）を後から区別する。

**`submissions` には列を足さない**（不変条件 I1）。提出の外に 1 行ずつ置き、
行が無い提出はファイルでの提出である。

Revision ID: e2c6b0f81a55
Revises: 7b3e9a15c4d2
Create Date: 2026-09-24 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "e2c6b0f81a55"
down_revision: str | None = "7b3e9a15c4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ide_submission_links",
        sa.Column("submission_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("ide_session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "recorded_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("submission_id"),
    )
    op.create_index(
        op.f("ix_ide_submission_links_task_id"), "ide_submission_links", ["task_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ide_submission_links_task_id"), table_name="ide_submission_links")
    op.drop_table("ide_submission_links")
