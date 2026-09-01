"""課題 ID にコースを混ぜる（#70）

課題 ID がキーだけから導かれていたので、2 つのコースが同じ自然な鍵
（`ex01/p1`）を使うと同じ ID になった。内容が違えば保存が落ち、**同じなら
既存の課題が黙って別のコースへ移る**。

ID を付け替える。**参照も一緒に付け替える** ── 採点結果は課題版を指しており
（P8）、片方だけ動かすと過去の成績が何の課題の点なのか辿れなくなる。

**ID は不透明な 36 文字の識別子なので、JSON の中は文字列として置換する。**
列だけを直すと、`document` に埋まった同じ ID（`q_matrix` の参照、採点の
文脈など）が古いまま残る。

`source_key` を持たない版は ID を導き直せないので**触らない**。導けないものを
推測で書き換えるより、古い ID のまま残すほうが安全である。

Revision ID: a1c4e77b90d2
Revises: e23567ff5f36
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "a1c4e77b90d2"
down_revision = "e23567ff5f36"
branch_labels = None
depends_on = None


# `aijudge_core.ids.derived_id` と同じ導出。**移行の中で複製する** ──
# 移行はその時点のコードを写し取ったものであって、あとからアプリの実装が
# 変わっても過去の移行の意味は変わってはいけない。
_NAMESPACE = uuid.UUID("6f9b1f6e-0d1a-4f1e-9b3a-8c5d2e7a4b10")


def _derived_id(prefix: str, *parts: str) -> str:
    key = "\x1e".join(parts)
    return f"{prefix}_{uuid.uuid5(_NAMESPACE, f'{prefix}:{key}').hex}"


# ID を持ちうる列。**列と JSON の両方を直す。**
_ID_COLUMNS = (
    ("tasks", "id"),
    ("task_versions", "id"),
    ("task_versions", "task_id"),
    ("submissions", "task_version_id"),
    ("grading_runs", "task_version_id"),
    ("task_checks", "version_id"),
    ("task_embeddings", "version_id"),
)
_JSON_COLUMNS = (
    ("tasks", "document"),
    ("task_versions", "document"),
    ("submissions", "document"),
    ("grading_runs", "document"),
    ("grading_jobs", "document"),
    ("outbox_events", "document"),
)


def _mapping(connection) -> dict[str, str]:
    """旧 ID → 新 ID。課題と版の両方。"""
    rows = connection.execute(
        sa.text(
            "select v.id, v.task_id, v.version, v.document, t.course_id "
            "from task_versions v join tasks t on t.id = v.task_id"
        )
    ).fetchall()
    mapping: dict[str, str] = {}
    for version_id, task_id, version, document, course_id in rows:
        payload = document if isinstance(document, dict) else json.loads(document)
        source_key = payload.get("source_key")
        if not source_key:
            # 導き直せない。**推測しない。**
            continue
        mapping[task_id] = _derived_id("tsk", course_id, source_key)
        mapping[version_id] = _derived_id("tsv", course_id, source_key, str(version))
    # 変わらないものは書き換えない（無駄な UPDATE を出さない）。
    return {old: new for old, new in mapping.items() if old != new}


def _apply(mapping: dict[str, str]) -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    for table, column in _ID_COLUMNS:
        # **列の有無まで見る。** 版を列に持たない表がある（`grading_jobs` は
        # `document` の中だけ）ので、表があることだけを確かめても足りない。
        if table not in tables or column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        for old, new in mapping.items():
            connection.execute(
                sa.text(f"update {table} set {column} = :new where {column} = :old"),
                {"new": new, "old": old},
            )
    # JSON は文字列として置換する。ID は 36 文字の不透明な識別子なので、
    # 他の値にたまたま含まれることは無い。
    for table, column in _JSON_COLUMNS:
        if table not in tables or column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        for old, new in mapping.items():
            connection.execute(
                sa.text(
                    f"update {table} set {column} = "
                    f"cast(replace(cast({column} as text), :old, :new) as jsonb) "
                    f"where cast({column} as text) like :like"
                )
                if connection.dialect.name == "postgresql"
                else sa.text(
                    f"update {table} set {column} = "
                    f"replace({column}, :old, :new) where {column} like :like"
                ),
                {"new": new, "old": old, "like": f"%{old}%"},
            )


def upgrade() -> None:
    mapping = _mapping(op.get_bind())
    _apply(mapping)


def downgrade() -> None:
    """新 ID → 旧 ID には戻せない。

    旧 ID は「コースを混ぜない導出」で、そちらは `source_key` から一意に
    導ける。**戻す側も同じ手順で作れる**ので、対称に書いておく。
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("select v.id, v.task_id, v.version, v.document from task_versions v")
    ).fetchall()
    mapping: dict[str, str] = {}
    for version_id, task_id, version, document in rows:
        payload = document if isinstance(document, dict) else json.loads(document)
        source_key = payload.get("source_key")
        if not source_key:
            continue
        mapping[task_id] = _derived_id("tsk", source_key)
        mapping[version_id] = _derived_id("tsv", source_key, str(version))
    _apply({old: new for old, new in mapping.items() if old != new})
