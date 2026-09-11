"""add the login label to oidc settings

Revision ID: 059a74b0906c
Revises: 38b7dd832491
Create Date: 2026-09-11 10:52:26.753807
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

# 自作の型（`UtcDateTime`）と方言固有の型（JSONB）を下の定義が参照する。
import aijudge_persistence.schema  # noqa: F401

revision: str = "059a74b0906c"
down_revision: str | None = "38b7dd832491"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ログイン画面のボタンの文言（#209）。
    #
    # **既存の行は空文字で入る。** 既定の文言はモデル（`aijudge_identity`）が
    # 持っており、読み出し側が空欄をそこへ戻す ── 移行で機関ごとの語彙を
    # 決め打ちすると、このリポジトリが公開物である以上どの機関にも合わない。
    op.add_column(
        "oidc_settings",
        sa.Column("login_label", sa.String(length=64), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("oidc_settings", "login_label")
