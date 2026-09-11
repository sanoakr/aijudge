"""add final_ratio to grading_runs and human_reviews

Revision ID: 9c2b7d4e1a08
Revises: 58a5a1368ffa
Create Date: 2026-09-12 10:20:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "9c2b7d4e1a08"
down_revision: str | None = "58a5a1368ffa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("grading_runs", sa.Column("final_ratio", sa.Float(), nullable=True))
    op.add_column("human_reviews", sa.Column("final_ratio", sa.Float(), nullable=True))
    _backfill()


def _backfill() -> None:
    """既存の行を埋める。**規則はドメインの関数に訊く**（#253）。

    値は `aijudge_core.final_score` が作るものでなければならない ── ここで
    規則を書き写すと、書き写した時点の規則で固まった列ができる。列と画面が
    食い違えば、得点の分布は静かに嘘をつく（分布は一覧のように 1 行ずつ
    見比べられないので、食い違いが目で見つからない）。

    **NULL は「数えない」。** 保留した採点（#235）と課題版が引けない採点が
    これに当たる。どちらも一覧に点として出ていない。
    """
    from aijudge_core import GradingRun, HumanReview, TaskVersion, final_score, score_withheld

    runs = sa.table(
        "grading_runs",
        sa.column("id", sa.String),
        sa.column("final_ratio", sa.Float),
        sa.column("document", sa.JSON),
    )
    reviews = sa.table(
        "human_reviews",
        sa.column("id", sa.String),
        sa.column("grading_run_id", sa.String),
        sa.column("final_ratio", sa.Float),
        sa.column("document", sa.JSON),
    )
    versions = sa.table(
        "task_versions",
        sa.column("id", sa.String),
        sa.column("document", sa.JSON),
    )
    connection = op.get_bind()

    def loaded(document: object) -> dict:
        if isinstance(document, str):
            document = json.loads(document)
        return document or {}  # type: ignore[return-value]

    by_version: dict[str, TaskVersion] = {
        row_id: TaskVersion.model_validate(loaded(document))
        for row_id, document in connection.execute(
            sa.select(versions.c.id, versions.c.document)
        ).fetchall()
    }
    reviewed: dict[str, HumanReview] = {}
    for run_id, document in connection.execute(
        sa.select(reviews.c.grading_run_id, reviews.c.document)
    ).fetchall():
        reviewed[run_id] = HumanReview.model_validate(loaded(document))

    for row_id, document in connection.execute(sa.select(runs.c.id, runs.c.document)).fetchall():
        run = GradingRun.model_validate(loaded(document))
        review = reviewed.get(row_id)
        version = by_version.get(str(run.context.task_version_id))
        if version is None:
            continue
        if not score_withheld(run, None):
            connection.execute(
                runs.update()
                .where(runs.c.id == row_id)
                .values(final_ratio=final_score(run, version, None).final)
            )
        if review is not None:
            connection.execute(
                reviews.update()
                .where(reviews.c.grading_run_id == row_id)
                .values(final_ratio=final_score(run, version, review).final)
            )


def downgrade() -> None:
    # **戻しても失うものは無い。** 列は採点と確認の文書から導けるので、
    # もう一度上げれば同じ値が埋まる。
    op.drop_column("human_reviews", "final_ratio")
    op.drop_column("grading_runs", "final_ratio")
