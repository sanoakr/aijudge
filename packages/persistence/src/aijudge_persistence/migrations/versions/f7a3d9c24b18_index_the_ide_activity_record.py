"""index the IDE activity record

行動記録（ADR 0023）の索引。**本体（イベント列とスナップショット）はファイル**に
置き、ここには IDE のセッションと、1 回の送信ごとのバッチの索引だけを置く。
バッチの主キー `(ide_session_id, seq)` が再送を重複させない。

入るのは表 2 つで、既存の表には触らない（不変条件 I4）。

Revision ID: f7a3d9c24b18
Revises: e2c6b0f81a55
Create Date: 2026-09-24 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "f7a3d9c24b18"
down_revision: str | None = "e2c6b0f81a55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UTC = aijudge_persistence.schema.UtcDateTime


def upgrade() -> None:
    op.create_table(
        "ide_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("started_at", _UTC(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=False),
        sa.Column("consented_at", _UTC(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ide_sessions_learner_course", "ide_sessions", ["learner_id", "course_id"], unique=False
    )
    op.create_table(
        "ide_event_batches",
        sa.Column("ide_session_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("received_at", _UTC(timezone=True), nullable=False),
        sa.Column("client_time", _UTC(timezone=True), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.PrimaryKeyConstraint("ide_session_id", "seq"),
    )


def downgrade() -> None:
    op.drop_table("ide_event_batches")
    op.drop_index("ix_ide_sessions_learner_course", table_name="ide_sessions")
    op.drop_table("ide_sessions")
