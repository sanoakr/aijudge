"""let a grading run carry more than one human review

Revision ID: b4f1c8e2d907
Revises: 9c2b7d4e1a08
Create Date: 2026-09-12 09:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "b4f1c8e2d907"
down_revision: str | None = "9c2b7d4e1a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """確定を訂正できるようにする（#275）。

    `human_reviews.grading_run_id` の UNIQUE を外し、時刻つきの索引に置き
    換える。読み出しは「その採点の最新の確認」を引くので、並べ替えが効く
    形にしておく。

    **なぜ外してよいか。** 「二度確定できると成績が二つ存在する」という
    理由で張られていたが、成績が二つになるのは**どちらが最終かを決めて
    いない**場合である。最新を成績とすると決めたので、積み上げてよい。

    TA も 1 件ずつなら確定できる（`require_grader`）ので、そこでの判断
    ちがいを教員が直せる必要がある。直せる人を教員に限る規則は経路側に
    ある。

    一致度（κ）には影響しない ── あちらの標本は blind 採点だけで、AI の
    判定を見ながら付けた確認は最初から入っていない
    （`ObservationRecord.usable_for_agreement`）。

    **SQLite は制約を落とすのに表を作り直す**ので `batch_alter_table` を
    通す（前例: `c790daf4057e`）。
    """
    with op.batch_alter_table("human_reviews") as batch:
        batch.drop_constraint("uq_reviews_run", type_="unique")
    op.create_index("ix_reviews_run_time", "human_reviews", ["grading_run_id", "reviewed_at"])


def downgrade() -> None:
    """戻すときは、訂正が 1 件も無いことが前提。

    **重複があれば落ちる。** 黙って消すと成績が変わる ── どれを残すかは
    運用の判断であって、移行が決めてよいことではない。
    """
    op.drop_index("ix_reviews_run_time", table_name="human_reviews")
    with op.batch_alter_table("human_reviews") as batch:
        batch.create_unique_constraint("uq_reviews_run", ["grading_run_id"])
