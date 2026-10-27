"""keep one skill observation per task

習熟度の観測を「学習者 × KC × 課題で 1 行」にする表（ADR 0032）。

以前は採点 1 回ごとに観測を足していた。演習で通るまで出し直した回数や
再採点が、そのまま習熟度を動かした。課題ごとに最終提出の結果だけを持ち、
習熟度はこの並びを畳み直して出す。

**既存の習熟度はここでは作り直さない。** 観測の表は空で始まり、新しい
採点から埋まる。過去の採点記録から作り直すのは `aijudge-admin skill rebuild`
（マイグレーションに採点の再生を載せると、配置のたびに長い処理が走る）。

Revision ID: c3e8a1f6d904
Revises: b7d1f4a9c2e6
Create Date: 2026-10-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "c3e8a1f6d904"
down_revision: str | None = "b7d1f4a9c2e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_observations",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("kc_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column(
            "submitted_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "document",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "learner_id", "kc_id", "task_id"),
    )


def downgrade() -> None:
    op.drop_table("skill_observations")
