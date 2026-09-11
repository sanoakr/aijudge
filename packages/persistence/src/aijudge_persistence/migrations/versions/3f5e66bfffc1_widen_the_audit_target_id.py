"""widen the audit target id

Revision ID: 3f5e66bfffc1
Revises: 059a74b0906c
Create Date: 2026-09-11 11:37:40.478842
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "3f5e66bfffc1"
down_revision: str | None = "059a74b0906c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 受講登録は「どのコースの誰か」の対でしか名指せず、64 字に入らない
    # （`crs_…:usr_…` で 73 字）。デモコースの自動登録が最初のログインで
    # 500 を返したのがこれ。**広げるだけで、既存の行は変わらない。**
    #
    # SQLite は `ALTER COLUMN ... TYPE` を受け付けないので batch モード
    # （作り直す方式）で通す ── 既存の移行（`c790daf4057e`）と同じ手。
    # なお SQLite は長さを強制しないので、効くのは PostgreSQL の側である。
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "target_id",
            existing_type=sa.VARCHAR(length=64),
            type_=sa.String(length=128),
            existing_nullable=False,
        )


def downgrade() -> None:
    # **戻すと 64 字を超える行が入らなくなる。** 既にある長い target_id は
    # 切り捨てられるので、戻す前に無いことを確かめること。
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.alter_column(
            "target_id",
            existing_type=sa.String(length=128),
            type_=sa.VARCHAR(length=64),
            existing_nullable=False,
        )
