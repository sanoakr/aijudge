"""protect grading runs from updates

採点結果（`grading_runs`）の不変性を **DB で**守る（ADR 0003・#408）。

ADR 0003 は採点結果を追記のみとしたが、守っていたのはアプリ層（`save` の
存在確認）だけだった。SQL を直接流す移行や運用の手作業、将来のリポジトリの
書き間違いは、何にも止められない。

**許す更新は 2 つだけ**:

- `superseded_by`（再採点で置き換わったことを記す。文書の同名のキーも一緒に動く）
- `final_ratio`（一覧に出ている点の写し。移行 `9c2b7d4e1a08` の埋め戻し）

それ以外の列・文書の中身が変わる UPDATE は例外にする。**DELETE は止めない** ──
コースの削除（学習者の提出が無いときだけ許す）が正規の経路としてある。

PostgreSQL だけに入れる。SQLite は開発用で、トリガの方言も違う。

**これより後の移行で採点文書を書き換える必要が出たら**、その移行の中で
`ALTER TABLE grading_runs DISABLE TRIGGER grading_runs_append_only` → 書き換え →
`ENABLE TRIGGER` とする（理由をその移行に書くこと）。

Revision ID: b3f7c1d9e2a4
Revises: a8d2e5f1c7b3
Create Date: 2026-09-26 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3f7c1d9e2a4"
down_revision: str | None = "a8d2e5f1c7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION grading_runs_append_only() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.submission_id IS DISTINCT FROM OLD.submission_id
       OR NEW.task_version_id IS DISTINCT FROM OLD.task_version_id
       OR NEW.subject_profile IS DISTINCT FROM OLD.subject_profile
       OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
       OR NEW.score_ratio IS DISTINCT FROM OLD.score_ratio
       OR NEW.confidence IS DISTINCT FROM OLD.confidence
       OR NEW.routing IS DISTINCT FROM OLD.routing
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.document - 'superseded_by') IS DISTINCT FROM (OLD.document - 'superseded_by')
    THEN
        RAISE EXCEPTION 'grading_runs is append-only (ADR 0003): run %', OLD.id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'only superseded_by and final_ratio may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER grading_runs_append_only BEFORE UPDATE ON grading_runs "
        "FOR EACH ROW EXECUTE FUNCTION grading_runs_append_only()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS grading_runs_append_only ON grading_runs")
    op.execute("DROP FUNCTION IF EXISTS grading_runs_append_only()")
