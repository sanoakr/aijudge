"""keep the IDE's autosave

ブラウザ IDE の自動保存（設計書 §6.5）。(学習者, 課題) ごとに 1 行を上書きで
持つ。提出でも行動記録でもなく、採点はこの表を読まない。

入るのは表 1 つだけで、既存の表には触らない（不変条件 I4）。

Revision ID: 7b3e9a15c4d2
Revises: d4a8f2c61e97
Create Date: 2026-09-24 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "7b3e9a15c4d2"
down_revision: str | None = "d4a8f2c61e97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ide_buffers",
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("suffix", sa.String(length=8), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("learner_id", "task_id"),
    )


def downgrade() -> None:
    op.drop_table("ide_buffers")
