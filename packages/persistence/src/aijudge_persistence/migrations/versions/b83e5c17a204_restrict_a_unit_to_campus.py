"""restrict a unit to campus

一部の問題セットを学内からだけ受け付ける（#333）。

入るのは表 1 つ、テナント単位の学内アドレス範囲（`campus_networks`）だけ。

**範囲はここに書かない。** このリポジトリは公開物で、学内の範囲は機関ごとに
違う ── 表を作るだけで、値はテナント管理者が画面から入れる。

課題ごとの可否（`Task.campus_only`）に列は要らない。`tasks` は模型を
`document` に入れており、列になっているのは並べ替えに使う `unit` /
`session` / `position` だけである ── 既定 false の真偽値は絞り込みにも
並べ替えにも使わないので、写しを作る理由が無い（`submissions.is_trial` は
SQL 側で数えるために写してある。あれは理由があっての例外）。

**既定は false。** 付け忘れて誰も出せない、より、付け忘れて外からも出せる、
のほうが取り返しがつく ── 前者は試験当日に全員が止まる。既存の課題は
`document` に鍵を持たないので、模型の既定がそのまま効く。

Revision ID: b83e5c17a204
Revises: a1c4f70b28de
Create Date: 2026-09-16 15:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema

revision: str = "b83e5c17a204"
down_revision: str | None = "a1c4f70b28de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campus_networks",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "cidrs",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("campus_networks")
