"""queue the IDE's run requests

ブラウザ IDE の試しの実行（ADR 0024）。**採点キュー（`grading_jobs`）とは
別の表**にする ── 相乗りすると、試験中の実行の山が採点を待たせ、採点の山が
学習者の画面を固める。

入るのは表 1 つだけで、既存の表には触らない（不変条件 I4）。

`uq_run_requests_one_in_flight` は**部分**一意索引である。1 人が同時に
待てるのは 1 件で、終わった要求は何件あってもよい。PostgreSQL・SQLite の
両方が部分索引を持つので、同じ定義が両方で効く。

Revision ID: d4a8f2c61e97
Revises: 5e1b9c0d7a42
Create Date: 2026-09-24 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "d4a8f2c61e97"
down_revision: str | None = "5e1b9c0d7a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IN_FLIGHT = sa.text("state IN ('queued', 'running')")


def upgrade() -> None:
    op.create_table(
        "run_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("task_version_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "finished_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "lease_expires_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "document",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_run_requests_one_in_flight",
        "run_requests",
        ["learner_id"],
        unique=True,
        postgresql_where=_IN_FLIGHT,
        sqlite_where=_IN_FLIGHT,
    )
    op.create_index(
        "ix_run_requests_state_created", "run_requests", ["state", "created_at"], unique=False
    )
    op.create_index(
        "ix_run_requests_learner_created",
        "run_requests",
        ["learner_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_requests_learner_created", table_name="run_requests")
    op.drop_index("ix_run_requests_state_created", table_name="run_requests")
    op.drop_index("uq_run_requests_one_in_flight", table_name="run_requests")
    op.drop_table("run_requests")
