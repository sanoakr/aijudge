"""確定の出所と、異議申立が確定を止める判定を固定する（#684）。

`Finalization` は「成績が確定した」事実で、`HumanReview`（教員が 1 件を読んだ）
とは別の記録である（ADR 0010）。κ の証拠になるのは後者だけなので、確定の側に
教員の ID やレビューの ID が紛れ込むと、誰も読んでいない提出が「読んで確定した」
と読めてしまう。この区別を守っているのは `Finalization._check_source` の 3 つの
検査だけで、どれを消してもスイートは通っていた。

異議申立（`ReviewRequest`）は、教員が答えるか再採点で終わるまで確定を止める。
**答え終わった依頼まで止め続けると**、その提出は自動確定でも一括確定でも永久に
「contested」として見送られる。止める側だけでなく、止めない側も固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    Finalization,
    FinalizationSource,
    GradingContext,
    GradingRun,
    ReviewRequest,
    Routing,
    auto_finalizable,
    blocks_finalization,
    bulk_finalizable,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    FinalizationId,
    GradingRunId,
    HumanReviewId,
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    UserId,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
RUN = GradingRunId("grn_" + "a" * 32)
ACTOR = UserId("usr_" + "f" * 32)
REVIEW = HumanReviewId("hrv_" + "e" * 32)
JUSTIFICATION = "提出物を読み、判定に同意しました。根拠は以上です。"


def _finalization(
    source: FinalizationSource,
    *,
    actor_id: UserId | None,
    review_id: HumanReviewId | None,
) -> Finalization:
    return Finalization(
        id=FinalizationId("fin_" + "1" * 32),
        grading_run_id=RUN,
        source=source,
        actor_id=actor_id,
        review_id=review_id,
        justification=JUSTIFICATION,
        finalized_at=NOW,
    )


# --------------------------------------------------------------------------
# 確定の出所ごとの、許される組と拒まれる組
# --------------------------------------------------------------------------


def test_an_instructor_review_finalization_names_both_the_actor_and_the_review() -> None:
    """個別確定は、誰が・どのレビューで確定したかを両方持つ。"""
    finalization = _finalization(
        FinalizationSource.INSTRUCTOR_REVIEW, actor_id=ACTOR, review_id=REVIEW
    )
    assert finalization.reviewed_individually


def test_an_instructor_review_finalization_without_its_review_is_refused() -> None:
    """`review_id` の無い個別確定は、「読んで確定した」の裏付けを持たない。

    測定側は `review_id` から `HumanReview` を辿るので、欠けていれば κ の
    標本から黙って落ちる（あるいは、読んでいない確定が個別確定に見える）。
    """
    with pytest.raises(ValidationError, match="must name its HumanReview"):
        _finalization(FinalizationSource.INSTRUCTOR_REVIEW, actor_id=ACTOR, review_id=None)


def test_an_instructor_review_finalization_without_its_actor_is_refused() -> None:
    with pytest.raises(ValidationError, match="must name its actor"):
        _finalization(FinalizationSource.INSTRUCTOR_REVIEW, actor_id=None, review_id=REVIEW)


def test_a_bulk_finalization_names_the_actor_but_no_review() -> None:
    """一括確定は教員が責任を取るが、個々の提出は読んでいない。"""
    finalization = _finalization(FinalizationSource.INSTRUCTOR_BULK, actor_id=ACTOR, review_id=None)
    assert not finalization.reviewed_individually


def test_a_bulk_finalization_that_points_at_a_review_is_refused() -> None:
    """一括確定に `review_id` を付けると、読んでいない提出が「読んだ」側に数えられる。"""
    with pytest.raises(ValidationError, match="reviews nothing individually"):
        _finalization(FinalizationSource.INSTRUCTOR_BULK, actor_id=ACTOR, review_id=REVIEW)


def test_a_bulk_finalization_without_its_actor_is_refused() -> None:
    with pytest.raises(ValidationError, match="must name its actor"):
        _finalization(FinalizationSource.INSTRUCTOR_BULK, actor_id=None, review_id=None)


def test_an_automatic_finalization_has_neither_actor_nor_review() -> None:
    finalization = _finalization(FinalizationSource.AUTOMATIC, actor_id=None, review_id=None)
    assert not finalization.reviewed_individually


@pytest.mark.parametrize(
    ("actor_id", "review_id"),
    [(ACTOR, None), (None, REVIEW), (ACTOR, REVIEW)],
    ids=["actor", "review", "both"],
)
def test_an_automatic_finalization_that_borrows_an_id_is_refused(
    actor_id: UserId | None, review_id: HumanReviewId | None
) -> None:
    """自動確定に誰かの ID を借りて書くと、「その教員が確定した」と読めてしまう。

    片方だけでも拒む ── 条件が `and` に化ける退行もここで捕まえる。
    """
    with pytest.raises(ValidationError, match="no actor and no review"):
        _finalization(FinalizationSource.AUTOMATIC, actor_id=actor_id, review_id=review_id)


# --------------------------------------------------------------------------
# 異議申立が確定を止める判定
# --------------------------------------------------------------------------


def _run() -> GradingRun:
    """暫定でも人採点待ちでもない、そのまま確定できる採点。"""
    criterion = CriterionId("crt_" + "1" * 32)
    return GradingRun(
        id=RUN,
        submission_id=SubmissionId("sub_" + "b" * 32),
        context=GradingContext(
            task_version_id=TaskVersionId("tsv_" + "2" * 32),
            subject_profile="cs_lang_c_intro",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "1" * 32),
                criterion_id=criterion,
                evaluator_result_id=EvaluatorResultId("evr_" + "d" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=2,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="テストを実行した。",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        created_at=NOW,
    )


def _request(
    *,
    resolved_by: HumanReviewId | None = None,
    closed_by_run: GradingRunId | None = None,
) -> ReviewRequest:
    return ReviewRequest(
        id=ReviewRequestId("rrq_" + "3" * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        grading_run_id=RUN,
        learner_id=UserId("usr_" + "4" * 32),
        reason="出力の形式は問題文どおりだと考えます。確認してください。",
        requested_at=NOW,
        resolved_by=resolved_by,
        closed_by_run=closed_by_run,
        closed_at=NOW if closed_by_run is not None else None,
    )


def test_a_pending_request_holds_both_automatic_and_bulk_finalization() -> None:
    """学習者が「人に見てほしい」と言っているものを、機械も一括も閉じない（ADR 0009）。"""
    run = _run()
    pending = _request()

    assert blocks_finalization(pending)
    assert not auto_finalizable(run, pending)
    assert not bulk_finalizable(run, pending)


def test_an_answered_request_no_longer_holds_finalization() -> None:
    """**教員が答えた依頼は確定を止めない。**

    止め続けると、その提出は自動確定でも一括確定でも永久に見送られ、
    画面には「contested」が残り続ける。
    """
    run = _run()
    answered = _request(resolved_by=REVIEW)

    assert not blocks_finalization(answered)
    assert auto_finalizable(run, answered)
    assert bulk_finalizable(run, answered)


def test_a_request_closed_by_a_regrade_no_longer_holds_finalization() -> None:
    """再採点で終わった依頼も止めない。学習者は新しい採点に改めて依頼できる。"""
    run = _run()
    closed = _request(closed_by_run=GradingRunId("grn_" + "9" * 32))

    assert not blocks_finalization(closed)
    assert auto_finalizable(run, closed)
    assert bulk_finalizable(run, closed)


def test_no_request_does_not_hold_finalization() -> None:
    assert not blocks_finalization(None)
