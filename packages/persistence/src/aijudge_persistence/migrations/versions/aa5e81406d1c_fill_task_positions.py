"""fill task positions

位置（`position`）の無い課題に、問題セットの末尾から作成順で位置を振る（#484）。

位置が空だと同じ問題セットの中で並びが決まらず、読み直すたびに入れ替わりえた
（SQLite と PostgreSQL でも順が違う）。これからは足すときに末尾へ振る
（`aijudge_core.position_for`）ので、空が残っているのはこれより前に作られた
課題だけである。

表の形は変えない。列（`tasks.position`）と文書（`tasks.document` の
`position`）の**両方**を書く ── 読み出しは文書から、並べ替えの索引は列から
なので、片方だけ書くと画面と索引が食い違う。

Revision ID: aa5e81406d1c
Revises: c4e8a2f6d1b7
Create Date: 2026-09-27 18:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "aa5e81406d1c"
down_revision: str | None = "c4e8a2f6d1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 版の無い課題の作成順。版より後ろに並べる（作られた時刻が分からない）。
_NO_VERSION = "9999"


def _loads(document: object) -> dict[str, Any]:
    if isinstance(document, str):
        document = json.loads(document)
    return dict(document) if isinstance(document, dict) else {}


def _as_key(value: object) -> str:
    """作成日時を比べられる文字列にする。SQLite は文字列、PostgreSQL は datetime で返す。"""
    if value is None:
        return _NO_VERSION
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def upgrade() -> None:
    """問題セットごとに、いまある最大の位置の次から作成順で振る。

    **作成順は版 1 の作成日時**（`task_versions.created_at` の最小）で見る。
    課題の表は作成日時を持たない。同着は ID で割る（`Task.sort_key` と同じ）。

    **位置のある課題は動かさない。** 教員が並べた順を、この移行が変えては
    いけない。
    """
    tasks = sa.table(
        "tasks",
        sa.column("id", sa.String),
        sa.column("course_id", sa.String),
        sa.column("unit", sa.String),
        sa.column("position", sa.Integer),
        sa.column("document", sa.JSON),
    )
    versions = sa.table(
        "task_versions",
        sa.column("task_id", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    connection = op.get_bind()

    created = {
        task_id: _as_key(first)
        for task_id, first in connection.execute(
            sa.select(versions.c.task_id, sa.func.min(versions.c.created_at)).group_by(
                versions.c.task_id
            )
        ).fetchall()
    }
    rows = connection.execute(
        sa.select(tasks.c.id, tasks.c.course_id, tasks.c.unit, tasks.c.position, tasks.c.document)
    ).fetchall()

    highest: dict[tuple[str, str | None], int] = {}
    missing: dict[tuple[str, str | None], list[tuple[str, str, object]]] = {}
    for task_id, course_id, unit, position, document in rows:
        group = (course_id, unit)
        if position is not None:
            highest[group] = max(highest.get(group, 0), position)
        else:
            missing.setdefault(group, []).append(
                (created.get(task_id, _NO_VERSION), task_id, document)
            )

    for group, entries in missing.items():
        next_position = highest.get(group, 0)
        for _created, task_id, document in sorted(entries, key=lambda e: (e[0], e[1])):
            next_position += 1
            updated = _loads(document) | {"position": next_position}
            connection.execute(
                tasks.update()
                .where(tasks.c.id == task_id)
                .values(position=next_position, document=updated)
            )


def downgrade() -> None:
    # **戻さない。** どの課題が空だったかは残していないし、振った位置は
    # 空より悪い値ではない（戻しても並びが決まらない状態に戻るだけ）。
    pass
