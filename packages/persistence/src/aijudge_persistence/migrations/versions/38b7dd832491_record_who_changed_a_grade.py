"""record who changed something that reaches a grade

3 つあるログのうちの監査ログ（ADR 0016）。運用ログは journald にあり 90 日で
消えるが、この表は学期を跨いで残す ── 成績への異議申立てはその後に来る。
DB に置くことで `/srv/aijudge` の restic バックアップ（DB ダンプ）に自動的に入る。

**追記のみ。** 更新も削除も経路を作らない（`grading_runs` と同じ理由、P8）。

Revision ID: 38b7dd832491
Revises: c790daf4057e
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import aijudge_persistence.schema

revision: str = "38b7dd832491"
down_revision: str | None = "c790daf4057e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("at", aijudge_persistence.schema.UtcDateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        # `user` か `system`。NULL で「不明」を表さない ── 自動確定に操作者が
        # いないのは欠落ではなく事実である。
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_at"), "audit_events", ["at"], unique=False)
    op.create_index(op.f("ix_audit_events_tenant_id"), "audit_events", ["tenant_id"], unique=False)
    op.create_index(
        op.f("ix_audit_events_actor_user_id"), "audit_events", ["actor_user_id"], unique=False
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(
        op.f("ix_audit_events_request_id"), "audit_events", ["request_id"], unique=False
    )
    # 「この提出に何が起きたか」を新しい順に引く経路。
    op.create_index("ix_audit_target", "audit_events", ["target_type", "target_id", "at"])
    # 「このテナントの最近の行為」「この行為の履歴」。
    op.create_index("ix_audit_tenant_at", "audit_events", ["tenant_id", "at"])
    op.create_index("ix_audit_tenant_action_at", "audit_events", ["tenant_id", "action", "at"])


def downgrade() -> None:
    op.drop_index("ix_audit_tenant_action_at", table_name="audit_events")
    op.drop_index("ix_audit_tenant_at", table_name="audit_events")
    op.drop_index("ix_audit_target", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_request_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_user_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_tenant_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_at"), table_name="audit_events")
    op.drop_table("audit_events")
