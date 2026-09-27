"""生成された課題の承認率の規則を固定する（S2、設計方針 §5）。

生成物だけを数える    手書きの課題を分母に入れると承認率がいくらでも高く出る。
測れていないは合格でない ADR 0005 と同じ規則を作問にも当てる。

レビュー状態の規則は `packages/authoring/tests/test_review_state_rules.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_admin import ApprovalRate, approval_rate
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
