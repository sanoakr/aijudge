"""add a lane to grading jobs

Revision ID: b7d1f4a9c2e6
Revises: a4c8e2f61b93
Create Date: 2026-10-16 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d1f4a9c2e6"
down_revision: str | None = "a4c8e2f61b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ジョブの列（`JobLane`・#627）。**既存の行は default で埋める** ── 書き起こしの
    # 列は付け替えでしか生まれないので、付け替える前の行はすべて default である。
    # 行の document には `lane` が無いが、型の既定が default なので同じ値で読める。
    # SQLite は既定値つきの列の追加と索引の作成が別の文で済むので、バッチは要らない。
    op.add_column(
        "grading_jobs",
        sa.Column("lane", sa.String(length=32), nullable=False, server_default="default"),
    )
    op.create_index(
        "ix_jobs_phase_lane_available",
        "grading_jobs",
        ["phase", "lane", "state", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_phase_lane_available", table_name="grading_jobs")
    op.drop_column("grading_jobs", "lane")
