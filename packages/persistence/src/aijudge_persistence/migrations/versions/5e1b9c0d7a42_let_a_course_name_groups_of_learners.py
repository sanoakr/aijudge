"""let a course name groups of learners

課題の出題先を名簿で絞る（追試・再試験。`docs/design/task-visibility.md`）。

入るのは表 2 つ。コースの中の名簿（`course_groups`）と、その 1 行ずつ
（`course_group_members`）。

課題ごとの出題先（`Task.audience_group_ids`）と、公開まで教員だけに見せるか
（`Task.confidential_until_open`）に列は要らない。`tasks` は模型を `document` に
入れており、どちらも絞り込みにも並べ替えにも使わない（`campus_only` の
b83e5c17a204 と同じ判断）。既存の課題は `document` に鍵を持たないので、模型の
既定（出題先なし＝全員・秘匿なし）がそのまま効く ── **今日までの見え方は
変わらない。**

Revision ID: 5e1b9c0d7a42
Revises: b83e5c17a204
Create Date: 2026-09-24 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e1b9c0d7a42"
down_revision: str | None = "b83e5c17a204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "course_groups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("course_id", "name", name="uq_course_groups_name"),
    )
    op.create_index(
        op.f("ix_course_groups_course_id"), "course_groups", ["course_id"], unique=False
    )
    op.create_index(
        op.f("ix_course_groups_tenant_id"), "course_groups", ["tenant_id"], unique=False
    )
    op.create_table(
        "course_group_members",
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["course_groups.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
    )
    op.create_index(
        "ix_course_group_members_user", "course_group_members", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_course_group_members_user", table_name="course_group_members")
    op.drop_table("course_group_members")
    op.drop_index(op.f("ix_course_groups_tenant_id"), table_name="course_groups")
    op.drop_index(op.f("ix_course_groups_course_id"), table_name="course_groups")
    op.drop_table("course_groups")
