"""add task drafts

承認待ちの課題を課題表の外に置く（#321）。

生成物は以前その場で課題として保存し、版を `IN_REVIEW` にして承認を待って
いた。それだと**承認より前に同一性が決まる** ── 課題キーから課題 ID が決まり、
提出も採点もそこにぶら下がる。生成物は提案であって確定ではない（P5）ので、
名前を含めて承認のときに決められる必要がある。

**移行前の承認待ちは引き継がない**（2026-09-15 決定）。この表は空で始まり、
`task_versions` に残る `IN_REVIEW` の行は一覧に出なくなる ── 運用機に残って
いる待ちは、課題の画面から削除して入れ直す（`docs/RUNNING.md`）。

Revision ID: feca9f28314a
Revises: c7d2e91f4a63
Create Date: 2026-09-15 09:27:47.328048
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "feca9f28314a"
down_revision: str | None = "c7d2e91f4a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_drafts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "document",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_drafts_course_id"), "task_drafts", ["course_id"], unique=False)
    op.create_index(op.f("ix_task_drafts_created_at"), "task_drafts", ["created_at"], unique=False)
    op.create_index(op.f("ix_task_drafts_kind"), "task_drafts", ["kind"], unique=False)
    op.create_index(op.f("ix_task_drafts_task_id"), "task_drafts", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_task_drafts_task_id"), table_name="task_drafts")
    op.drop_index(op.f("ix_task_drafts_kind"), table_name="task_drafts")
    op.drop_index(op.f("ix_task_drafts_created_at"), table_name="task_drafts")
    op.drop_index(op.f("ix_task_drafts_course_id"), table_name="task_drafts")
    op.drop_table("task_drafts")
