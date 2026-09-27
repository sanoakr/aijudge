"""課題版のレビュー状態の規則（`TaskRepository.record_review`）を固定する。

以前は `aijudge_course_admin.task_review` の `approve`・`reject`・`pending_reviews`（保存層を
呼ぶだけの包み）を通して確かめていた。承認・却下の入口が画面の下書きだけになり
（#522・ADR 0019）、包みは使われなくなったので消した。**規則は保存層に残る**ので、
ここで直に確かめる。

理由なく却下できない  却下理由は作問改善の材料。
二度は変えられない    承認済みを後から却下できると、出題済みの課題が
                      「承認されていない」ことになる。やり直しは新しい版（P8）。
レビューは問題文を触らない `save_version` と別の口にしてある。
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


def test_generated_tasks_wait_in_the_queue() -> None:
    repository = _repo(
        _version("a", generated=True, state=ReviewState.IN_REVIEW),
        _version("b", generated=False, state=ReviewState.APPROVED),
    )
    waiting = repository.list_versions_in_review()
    assert [v.id for v in waiting] == [TaskVersionId("tsv_" + "a" * 32)]


def test_approving_publishes_the_version() -> None:
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)

    updated = repository.record_review(version.id, approved=True, reviewer=INSTRUCTOR, reason=None)
    assert updated.provenance.review_state is ReviewState.APPROVED
    assert updated.provenance.reviewed_by == INSTRUCTOR
    assert updated.is_published
    assert repository.list_versions_in_review() == ()


def test_rejecting_keeps_the_reason() -> None:
    """**却下理由は捨てない**（設計方針 §5）。生成改善の材料になる。"""
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)

    updated = repository.record_review(
        version.id, approved=False, reviewer=INSTRUCTOR, reason="入出力の形式が課題文にない"
    )
    assert updated.provenance.review_state is ReviewState.REJECTED
    assert updated.provenance.reject_reason == "入出力の形式が課題文にない"


def test_rejecting_without_a_reason_is_refused() -> None:
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    with pytest.raises(ValueError, match="理由"):
        repository.record_review(version.id, approved=False, reviewer=INSTRUCTOR, reason="   ")


def test_a_decided_version_cannot_be_decided_again() -> None:
    """やり直しは新しい版から（P8）。

    後から覆せると、既に出題した課題が「承認されていない」ことになりうる。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    repository.record_review(version.id, approved=True, reviewer=INSTRUCTOR, reason=None)

    with pytest.raises(ValueError, match="already approved"):
        repository.record_review(
            version.id, approved=False, reviewer=INSTRUCTOR, reason="やっぱり駄目"
        )


def test_reviewing_does_not_let_the_statement_change() -> None:
    """レビューの口は問題文を触らない。

    同じ口にすると、レビューのつもりで出題済みの課題が黙って変わる。
    問題文の差し替えは `save_version` を通り、そこは不変性が拒む（P8）。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    repository.record_review(version.id, approved=True, reviewer=INSTRUCTOR, reason=None)

    with pytest.raises(TaskImmutabilityViolation):
        repository.save_version(version.model_copy(update={"statement": "## 別の課題 ##\n\n別"}))


def test_approving_is_not_blocked_by_immutability() -> None:
    """**承認そのものは通る。** レビュー状態は採点の基準ではない。

    `substantive` がレビューの項目を含んでいた頃は、承認した瞬間に
    「内容が違う」と拒否されてレビューが成立しなかった。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    approved = repository.record_review(version.id, approved=True, reviewer=INSTRUCTOR, reason=None)
    # 保存し直しても弾かれない（同じ課題の同じ内容だから）。
    repository.save_version(approved)
