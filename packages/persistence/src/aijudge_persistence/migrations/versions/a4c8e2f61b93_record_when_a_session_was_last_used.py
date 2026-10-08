"""record when a session was last used

Revision ID: a4c8e2f61b93
Revises: d5b8e3a7c0f2
Create Date: 2026-10-08 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "a4c8e2f61b93"
down_revision: str | None = "d5b8e3a7c0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 「いま使っている人数」を数えるための最終操作の時刻。**既存の行は空のまま** ──
    # 過去の操作は分からないので、作った時刻で埋めない（空は「まだ操作の記録が無い」）。
    # SQLite は列の追加と索引の作成が別の文で済むので、バッチは要らない。
    op.add_column(
        "sessions",
        sa.Column("last_seen_at", aijudge_persistence.schema.Timestamp, nullable=True),
    )
    op.create_index("ix_sessions_last_seen_at", "sessions", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_last_seen_at", table_name="sessions")
    op.drop_column("sessions", "last_seen_at")
