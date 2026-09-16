"""record how mastery moved

習熟度の推移を残す（#328）。

`skill_states` は最新の 1 行を持ち替える推定値なので、そこからは推移が
読めない。学期の途中で「この KC は上がっているのか」を言うにはここが要る
── 根拠（`SkillEvidence`）は最新 20 件に切られており、しかも BKT は観測列を
畳むので、後から遡って再現できない。

**過去は埋められない。** この表は空で始まり、記録はこの版を当てた時点から
積み上がる。画面はそれを黙らない（「記録はここから」と書く）── 空の図を
「まだ動いていない」と読ませるのが、いちばん高くつく。

Revision ID: a1c4f70b28de
Revises: feca9f28314a
Create Date: 2026-09-16 00:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "a1c4f70b28de"
down_revision: str | None = "feca9f28314a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_points",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("kc_id", sa.String(length=64), nullable=False),
        sa.Column("mastery", sa.Float(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=32), nullable=False),
        sa.Column(
            "recorded_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_skill_point_tenant_learner_at",
        "skill_points",
        ["tenant_id", "learner_id", "recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_skill_point_tenant_learner_at", table_name="skill_points")
    op.drop_table("skill_points")
