"""課題版のレビュー状態の規則を固定する。

承認・却下の入口は画面の下書きだけになり（#522・ADR 0019）、課題版のレビューを
書き戻す口（`record_review`）と、その包み（`approve`・`reject`）は消した。残る規則は
3 つ。

承認待ちは一覧に出る        古い版に残る承認待ちも数えられる（`list_versions_in_review`）。
問題文は書き換えられない    保存済みの版は不変（P8・`save_version`）。
レビューの結果は内容ではない 承認済みで入れ直しても「内容が違う」にしない（`REVIEW_FIELDS`）。
却下には理由が要る          `Provenance` の検証（core）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_authoring import InMemoryTaskRepository, TaskImmutabilityViolation
from aijudge_core import (
    Provenance,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId

INSTRUCTOR = UserId("usr_" + "1" * 32)
AUTHOR = UserId("usr_" + "2" * 32)


def _version(suffix: str, *, generated: bool, state: ReviewState) -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + suffix * 32),
        task_id=TaskId("tsk_" + suffix * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="## 課題 ##\n\n書きなさい。",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + suffix * 32),
                code="correctness",
                title="正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
            ),
        ),
        max_score=100.0,
        provenance=Provenance(
            authored_by=AUTHOR,
            generated_by="stub" if generated else None,
            generation_prompt_version="task_draft_ja@1" if generated else None,
            review_state=state,
            reject_reason="入出力が曖昧" if state is ReviewState.REJECTED else None,
        ),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _repo(*versions: TaskVersion) -> InMemoryTaskRepository:
    repository = InMemoryTaskRepository()
    for version in versions:
        repository.save_version(version)
    return repository


def test_versions_awaiting_review_are_listed() -> None:
    repository = _repo(
        _version("a", generated=True, state=ReviewState.IN_REVIEW),
        _version("b", generated=False, state=ReviewState.APPROVED),
    )
    waiting = repository.list_versions_in_review()
    assert [v.id for v in waiting] == [TaskVersionId("tsv_" + "a" * 32)]


def test_the_statement_of_a_stored_version_cannot_change() -> None:
    """保存済みの版の問題文は変えられない。訂正は新しい版（P8）。"""
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)

    with pytest.raises(TaskImmutabilityViolation):
        repository.save_version(version.model_copy(update={"statement": "## 別の課題 ##\n\n別"}))


def test_the_review_outcome_is_not_part_of_the_content() -> None:
    """**同じ版を承認済みで入れ直しても弾かない。** レビューの結果は採点の基準ではない。

    `substantive` がレビューの項目を含んでいた頃は、承認した瞬間に
    「内容が違う」と拒否されてレビューが成立しなかった。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    approved = version.model_copy(
        update={
            "provenance": version.provenance.model_copy(
                update={"review_state": ReviewState.APPROVED, "reviewed_by": INSTRUCTOR}
            )
        }
    )
    repository.save_version(approved)


def test_a_rejection_needs_a_reason() -> None:
    """却下理由は作問改善の材料。理由の無い却下は作れない（core の検証）。"""
    with pytest.raises(ValueError, match="reject_reason"):
        Provenance(authored_by=AUTHOR, generated_by="stub", review_state=ReviewState.REJECTED)
