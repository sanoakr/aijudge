"""add is_trial to submissions

Revision ID: 58a5a1368ffa
Revises: 3f5e66bfffc1
Create Date: 2026-09-11 15:45:16.022222
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "58a5a1368ffa"
down_revision: str | None = "3f5e66bfffc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("is_trial", sa.Boolean(), server_default="0", nullable=False),
    )
    op.create_index(op.f("ix_submissions_is_trial"), "submissions", ["is_trial"], unique=False)

    # 既存の行を埋める。**規則は `Submission.is_trial` と同じでなければ
    # ならない** ── `submitted_as` が学習者でない、または `is_demo` が真。
    #
    # どちらの欄も後から入ったので、古い文書には無い（#108・#194）。
    # **無いものは学習者の提出として埋める** ── 入る前の提出には、教員の
    # 試行もデモコースも概念として存在しなかった。
    #
    # JSON の読み方が方言で違うので、ここは移行の中で 1 行ずつ判定する。
    # 提出は SUBMITTED 以降不変なので、埋めた値が後から狂うことはない。
    submissions = sa.table(
        "submissions",
        sa.column("id", sa.String),
        sa.column("is_trial", sa.Boolean),
        sa.column("document", sa.JSON),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(submissions.c.id, submissions.c.document)).fetchall()
    for row_id, document in rows:
        if isinstance(document, str):
            document = json.loads(document)
        document = document or {}
        submitted_as = document.get("submitted_as", "learner")
        is_trial = submitted_as != "learner" or bool(document.get("is_demo", False))
        if is_trial:
            connection.execute(
                submissions.update().where(submissions.c.id == row_id).values(is_trial=True)
            )


def downgrade() -> None:
    # **戻しても失うものは無い。** 列は `document` の中身から導けるので、
    # もう一度上げれば同じ値が埋まる。
    op.drop_index(op.f("ix_submissions_is_trial"), table_name="submissions")
    op.drop_column("submissions", "is_trial")
