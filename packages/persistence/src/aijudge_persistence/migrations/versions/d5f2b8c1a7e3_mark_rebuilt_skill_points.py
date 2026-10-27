"""mark rebuilt skill points

習熟度の作り直し（`aijudge-admin skill rebuild`）が書いた推移の点に、その作り直しの
時刻を持たせる列（ADR 0032 §5）。

作り直しは、過去の採点を提出の順に再生し、そのときどきの値を**採点の時刻で**点に
書く。古い数え方の点は消さない（P8）── 描くときに、最新の作り直しより前は作り直した
点を、それ以降はふだんの点を使う（`aijudge_core.effective_points`）。既存の点は NULL
（ふだんの点）。

Revision ID: d5f2b8c1a7e3
Revises: c3e8a1f6d904
Create Date: 2026-10-28 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 自作の型（`UtcDateTime`）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "d5f2b8c1a7e3"
down_revision: str | None = "c3e8a1f6d904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("skill_points") as batch:
        batch.add_column(
            sa.Column(
                "rebuilt_at",
                aijudge_persistence.schema.UtcDateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("skill_points") as batch:
        batch.drop_column("rebuilt_at")
