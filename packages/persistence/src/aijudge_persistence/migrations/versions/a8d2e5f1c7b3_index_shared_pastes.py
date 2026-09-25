"""index shared pastes

外からの大きな貼り付けの**指紋だけ**を残す索引（2026-09-25・`aijudge_ide.PasteMark`）。
学習者をまたいで同じ内容の貼り付けを見つけ、作業の記録に「ほかの学生と同じ内容の
貼り付け」の目印を付けるためにある。中身は持たない（中身は記録の本体のファイル）。

入るのは表 1 つで、既存の表には触らない（不変条件 I4）。

Revision ID: a8d2e5f1c7b3
Revises: f7a3d9c24b18
Create Date: 2026-09-25 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d2e5f1c7b3"
down_revision: str | None = "f7a3d9c24b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ide_paste_marks",
        sa.Column("ide_session_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("learner_id", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("t", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("ide_session_id", "seq", "position"),
    )
    op.create_index(
        "ix_ide_paste_marks_course_hash",
        "ide_paste_marks",
        ["course_id", "content_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ide_paste_marks_course_hash", table_name="ide_paste_marks")
    op.drop_table("ide_paste_marks")
