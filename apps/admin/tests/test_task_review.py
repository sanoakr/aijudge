"""生成された課題の教員レビューの規則を固定する（S2、設計方針 §5）。

固定したいのは 5 つ。

理由なく却下できない 却下理由は作問改善の材料で、Phase 4 の分母でもある。
二度は変えられない  承認済みを後から却下できると、出題済みの課題が
                    「承認されていない」ことになる。やり直しは新しい版（P8）。
レビューは問題文を触らない `save_version` と別の口にしてある。
生成物だけを数える  手書きの課題を分母に入れると承認率がいくらでも高く出る。
測れていないは合格でない ADR 0005 と同じ規則を作問にも当てる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_admin import ApprovalRate, approval_rate, approve, pending_reviews, reject
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
        subject_profile="cs_intro_c",
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


# -- 導線 -------------------------------------------------------------------


def test_generated_tasks_wait_in_the_queue() -> None:
    repository = _repo(
        _version("a", generated=True, state=ReviewState.IN_REVIEW),
        _version("b", generated=False, state=ReviewState.APPROVED),
    )
    waiting = pending_reviews(repository)
    assert [v.id for v in waiting] == [TaskVersionId("tsv_" + "a" * 32)]


def test_approving_publishes_the_version() -> None:
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)

    updated = approve(repository, version.id, reviewer=INSTRUCTOR)
    assert updated.provenance.review_state is ReviewState.APPROVED
    assert updated.provenance.reviewed_by == INSTRUCTOR
    assert updated.is_published
    assert pending_reviews(repository) == ()


def test_rejecting_keeps_the_reason() -> None:
    """**却下理由は捨てない**（設計方針 §5）。生成改善の材料になる。"""
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)

    updated = reject(
        repository, version.id, reviewer=INSTRUCTOR, reason="入出力の形式が課題文にない"
    )
    assert updated.provenance.review_state is ReviewState.REJECTED
    assert updated.provenance.reject_reason == "入出力の形式が課題文にない"


def test_rejecting_without_a_reason_is_refused() -> None:
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    with pytest.raises(ValueError, match="理由"):
        reject(repository, version.id, reviewer=INSTRUCTOR, reason="   ")


def test_a_decided_version_cannot_be_decided_again() -> None:
    """やり直しは新しい版から（P8）。

    後から覆せると、既に出題した課題が「承認されていない」ことになりうる。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    approve(repository, version.id, reviewer=INSTRUCTOR)

    with pytest.raises(ValueError, match="already approved"):
        reject(repository, version.id, reviewer=INSTRUCTOR, reason="やっぱり駄目")


def test_reviewing_does_not_let_the_statement_change() -> None:
    """レビューの口は問題文を触らない。

    同じ口にすると、レビューのつもりで出題済みの課題が黙って変わる。
    問題文の差し替えは `save_version` を通り、そこは不変性が拒む（P8）。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    approve(repository, version.id, reviewer=INSTRUCTOR)

    with pytest.raises(TaskImmutabilityViolation):
        repository.save_version(version.model_copy(update={"statement": "## 別の課題 ##\n\n別"}))


def test_approving_is_not_blocked_by_immutability() -> None:
    """**承認そのものは通る。** レビュー状態は採点の基準ではない。

    `substantive` がレビューの項目を含んでいた頃は、承認した瞬間に
    「内容が違う」と拒否されてレビューが成立しなかった。
    """
    version = _version("a", generated=True, state=ReviewState.IN_REVIEW)
    repository = _repo(version)
    approved = approve(repository, version.id, reviewer=INSTRUCTOR)
    # 保存し直しても弾かれない（同じ課題の同じ内容だから）。
    repository.save_version(approved)


# -- 承認率 -----------------------------------------------------------------


def test_hand_written_tasks_are_not_counted() -> None:
    """**教員が自分で書いた課題を分母に入れない。** 当然承認されるので、
    混ぜると承認率がいくらでも高く出る。"""
    rate = approval_rate(
        (
            _version("a", generated=True, state=ReviewState.APPROVED),
            _version("b", generated=True, state=ReviewState.REJECTED),
            _version("c", generated=False, state=ReviewState.APPROVED),
            _version("d", generated=False, state=ReviewState.APPROVED),
        )
    )
    assert rate.approved == 1
    assert rate.rejected == 1
    assert rate.rate == 0.5


def test_pending_tasks_are_not_in_the_denominator() -> None:
    """まだ落ちていないものを「落ちなかった」に数えない。"""
    rate = ApprovalRate(approved=3, rejected=1, pending=10)
    assert rate.decided == 4
    assert rate.rate == 0.75


def test_a_small_sample_is_not_a_pass() -> None:
    """ADR 0005 と同じ規則を作問にも当てる。3 件中 2 件は 67% の証拠ではない。"""
    assert ApprovalRate(approved=2, rejected=1, pending=0).verdict == "NOT_MEASURED"
    assert (
        "測れていないことは合格ではありません"
        in ApprovalRate(approved=2, rejected=1, pending=0).render()
    )


def test_the_gate_is_sixty_percent() -> None:
    assert ApprovalRate(approved=18, rejected=12, pending=0).verdict == "PASS"
    assert ApprovalRate(approved=17, rejected=13, pending=0).verdict == "FAIL"
