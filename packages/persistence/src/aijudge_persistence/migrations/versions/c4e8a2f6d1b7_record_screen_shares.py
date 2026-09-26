"""record screen shares

試験中の画面の共有の状態（ADR 0027・#444）。セッションごとに最後の 1 つだけ。
共有が止まっている間の手動の提出を断るために読む。入るのは表 1 つで、既存の表には
触らない（不変条件 I4）。

Revision ID: c4e8a2f6d1b7
Revises: b3f7c1d9e2a4
Create Date: 2026-09-27 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import aijudge_persistence.schema

revision: str = "c4e8a2f6d1b7"
down_revision: str | None = "b3f7c1d9e2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ide_screen_shares",
        sa.Column("ide_session_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("surface", sa.String(length=16), nullable=True),
        sa.Column("updated_at", aijudge_persistence.schema.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ide_session_id"),
    )


def downgrade() -> None:
    op.drop_table("ide_screen_shares")
