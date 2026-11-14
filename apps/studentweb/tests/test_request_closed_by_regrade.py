"""前の採点に出した再確認の依頼が、再採点で終了したことを学習者に伝える（2026-11-13）。

依頼はその採点への申し立てなので、採点が置き換わったら「再採点により終了」として
閉じる（教員が答えたことにはしない）。学習者には終わったことと、新しい採点に
改めて依頼できることを示す。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Provenance,
    ReviewRequest,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    HumanReviewId,
    ReviewRequestId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_studentweb.visibility import build_result_view

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
OLD_RUN = GradingRunId("grn_" + "a" * 32)
NEW_RUN = GradingRunId("grn_" + "c" * 32)
SUBMISSION = SubmissionId("sub_" + "5" * 32)
TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "aijudge_studentweb"
    / "templates"
    / "submission.html"
)


def _version() -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "3" * 32),
        task_id=TaskId("tsk_" + "4" * 32),
        version=2,
        subject_profile="cs_lang_c_intro",
        statement="問題文",
        criteria=(
            RubricCriterion(
                id=CRITERION,
                code="explain_sort",
                title="選択ソートの説明",
                description="コメントで説明する",
                weight=1.0,
                levels=(
                    RubricLevel(
                        level=0, label="未達", descriptor="説明できていない", score_ratio=0.0
                    ),
                    RubricLevel(
                        level=1, label="達成", descriptor="説明できている", score_ratio=1.0
                    ),
                ),
            ),
        ),
        max_score=10.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )


def _new_run() -> GradingRun:
    return GradingRun(
        id=NEW_RUN,
        submission_id=SUBMISSION,
        context=GradingContext(
            task_version_id=TaskVersionId("tsv_" + "3" * 32),
            subject_profile="cs_lang_c_intro",
            rubric_version="v2",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "7" * 32),
                criterion_id=CRITERION,
                evaluator_result_id=EvaluatorResultId("evr_" + "8" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=1,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="5 か所とも手順を説明している。",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        created_at=NOW,
    )


def _closed_request() -> ReviewRequest:
    return ReviewRequest(
        id=ReviewRequestId("rrq_" + "1" * 32),
        submission_id=SUBMISSION,
        grading_run_id=OLD_RUN,
        learner_id=UserId("usr_" + "9" * 32),
        reason="説明が「一部」ですが、5 か所とも手順を書いたつもりです。",
        requested_at=NOW,
        closed_by_run=NEW_RUN,
        closed_at=NOW,
    )


def test_the_learner_is_told_the_old_request_ended_and_may_ask_again() -> None:
    view = build_result_view(_new_run(), _version(), None, closed_request=_closed_request())

    assert view.request_closed_by_regrade
    # 新しい採点に対しては依頼できる。
    assert view.can_request_review
    assert not view.requested


def test_the_notice_goes_once_a_new_request_is_made() -> None:
    """新しい採点に改めて出したら、そちらの状態（依頼済み）を見せる。"""
    fresh = _closed_request().model_copy(
        update={
            "id": ReviewRequestId("rrq_" + "2" * 32),
            "grading_run_id": NEW_RUN,
            "closed_by_run": None,
            "closed_at": None,
        }
    )
    view = build_result_view(
        _new_run(), _version(), None, request=fresh, closed_request=_closed_request()
    )

    assert not view.request_closed_by_regrade
    assert view.requested


def test_the_page_says_so_in_words() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert "view.request_closed_by_regrade" in text
    assert "再採点されたため、この依頼は終了しました。" in text


def test_a_request_is_either_answered_or_closed_by_a_regrade() -> None:
    """**2 つの終わり方を混ぜない**（ADR 0010）。教員の確認は一致度の証拠になり、
    再採点での終了はならない。"""
    answered = HumanReviewId("hrv_" + "9" * 32)
    with pytest.raises(ValueError, match="either answered or closed"):
        ReviewRequest.model_validate(_closed_request().model_dump() | {"resolved_by": answered})
    with pytest.raises(ValueError, match="go together"):
        ReviewRequest.model_validate(_closed_request().model_dump() | {"closed_at": None})
