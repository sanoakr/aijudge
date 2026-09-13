"""コースが使う知識要素を明示にする（#289）

`courses.knowledge_components` が空のとき「名前空間の全部」と読んでいた。
作ったばかりのコースに 987 件が登録されているように見え、教員が責任を
持って選んだ語彙にならない。以後、空は「何も使わない」を意味する。

既存のコースで空のものには、**そのコースの課題が問うている知識要素**を
書き込む。全部を書き込むと問題をそのまま固定することになり、何も書かないと
既存の課題の訂正が「範囲外」で止まる（`kc.assert_registered`）。課題が
使っているものだけが、教員が実際に選んだ語彙である。

Revision ID: c7d2e91f4a63
Revises: b4f1c8e2d907
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d2e91f4a63"
down_revision: str | None = "b4f1c8e2d907"
branch_labels = None
depends_on = None


# 列の型を方言ごとに合わせる（SQLite は JSON、PostgreSQL は JSONB）。素の
# `text()` に文字列を渡すと PostgreSQL 側で型が合わない。
_COURSES = sa.table(
    "courses",
    sa.column("id", sa.String),
    sa.column(
        "knowledge_components",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
    ),
)


def _loads(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def upgrade() -> None:
    connection = op.get_bind()
    key_by_id = {
        row.id: row.key
        for row in connection.execute(sa.text("select id, key from knowledge_components"))
    }
    used: dict[str, set[str]] = {}
    versions = connection.execute(
        sa.text(
            "select t.course_id, v.document from task_versions v join tasks t on t.id = v.task_id"
        )
    )
    for course_id, document in versions:
        doc = _loads(document) or {}
        for entry in doc.get("q_matrix") or []:
            key = key_by_id.get(entry.get("kc_id"))
            if key:
                used.setdefault(course_id, set()).add(key)

    courses = connection.execute(sa.text("select id, knowledge_components from courses"))
    for course_id, current in courses:
        if _loads(current):
            continue  # 既に選んでいるコースは触らない
        keys = sorted(used.get(course_id, ()))
        if not keys:
            continue
        connection.execute(
            _COURSES.update().where(_COURSES.c.id == course_id).values(knowledge_components=keys)
        )


def downgrade() -> None:
    # 書き込んだ範囲は「課題が使っているもの」で、戻しても害は無い（旧コードは
    # 空を全部と読むだけ）。消さない。
    pass
