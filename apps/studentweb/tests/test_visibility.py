"""学習者に何を見せるかの規則を、純関数のまま固定する。

ここで確かめるのは 1 つ ── **採点できなかった観点があるあいだ、総合点を
出さない**（P2 の劣化動作）。決定的評価の結果は返す。

残った観点だけを比例配分した合計は正しい数字だが、欠けている観点を知らない
学習者には到達度として読まれる。観点の一覧に「確認中」と出ていても、大きく
出る数字の方が先に目に入る。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    HumanReview,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    HumanReviewId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_studentweb.visibility import build_result_view

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
CORRECTNESS = CriterionId("crt_" + "1" * 32)
READABILITY = CriterionId("crt_" + "2" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "3" * 32)


def _levels() -> tuple[RubricLevel, ...]:
    return (
        RubricLevel(level=0, label="不可", descriptor="満たさない", score_ratio=0.0),
        RubricLevel(level=3, label="達成", descriptor="満たす", score_ratio=1.0),
    )


def _task_version() -> TaskVersion:
    """観点 2 つ ── テスト実行（決定的）と読みやすさ（AI）。"""
    return TaskVersion(
        id=TASK_VERSION,
        task_id=TaskId("tsk_" + "4" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="問題文",
        criteria=(
            RubricCriterion(
                id=CORRECTNESS,
                code="correctness",
                title="出力の正しさ",
                description="テスト実行で判定する",
                weight=0.7,
                levels=_levels(),
            ),
            RubricCriterion(
                id=READABILITY,
                code="readability",
                title="読みやすさ",
                description="AI が判定する",
                weight=0.3,
                levels=_levels(),
            ),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "5" * 32)),
        created_at=NOW,
    )


def _run_missing_the_ai_criterion() -> GradingRun:
    """S6 が止まっていて AI 観点が採点できなかった採点。

    残った観点の重みは比例配分され（1.0 になる）、未採点の観点が記録され、
    振り分けは必ずレビュー行きになる（core の検証が強制する）。
    """
    return GradingRun(
        id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        context=GradingContext(
            task_version_id=TASK_VERSION,
            subject_profile="cs_intro_c",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId(new_id("cs")),
                criterion_id=CORRECTNESS,
                evaluator_result_id=EvaluatorResultId(new_id("evr")),
                kind=EvaluatorKind.DETERMINISTIC,
                level=3,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="テストケース 5 件すべてに正しい出力を返しました。",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.REVIEW_REQUIRED,
        unscored_criteria=(READABILITY,),
        created_at=NOW,
    )


def test_the_overall_score_is_withheld_while_a_criterion_is_unscored() -> None:
    """**100% とは出さない。** 読みやすさは誰も見ていない。"""
    view = build_result_view(_run_missing_the_ai_criterion(), _task_version(), None)

    assert view.score_ratio is None
    assert view.score_withheld


def test_the_deterministic_result_is_still_returned() -> None:
    """伏せるのは合計だけ。テスト実行の合否は確定した事実で、伏せる理由が無い。"""
    view = build_result_view(_run_missing_the_ai_criterion(), _task_version(), None)

    rows = {row.criterion.code: row for row in view.criteria}
    assert rows["correctness"].pending is False
    assert rows["correctness"].label == "達成"
    assert "テストケース 5 件" in (rows["correctness"].rationale or "")
    # 採点できなかった観点は「確認中」として残す。消すと欠けが見えない。
    assert rows["readability"].pending is True
    assert rows["readability"].label == "確認中"


def test_the_learner_cannot_contest_a_grade_that_is_not_finished() -> None:
    """依頼する対象がまだ無い。教員が欠けを埋めるのが先。"""
    view = build_result_view(_run_missing_the_ai_criterion(), _task_version(), None)

    assert not view.can_request_review
    assert view.request_reason == "採点できなかった観点があります。担当教員が確認します。"


def test_the_score_comes_back_once_the_instructor_has_filled_the_gap() -> None:
    """保留は「教員が見るまで」。確定すれば総合点を出す。"""
    run = _run_missing_the_ai_criterion()
    review = HumanReview(
        id=HumanReviewId(new_id("hrv")),
        grading_run_id=run.id,
        grader_id=UserId("usr_" + "6" * 32),
        comment="読みやすさを確認しました。変数名は追えるので達成とします。",
        reviewed_at=NOW,
    )

    view = build_result_view(run, _task_version(), review)

    assert view.score_ratio is not None
    assert not view.score_withheld
    assert view.confirmed


def test_a_complete_run_shows_its_score() -> None:
    """欠けが無ければ従来どおり。保留は劣化動作のときだけ。"""
    task = _task_version()
    run = _run_missing_the_ai_criterion()
    complete = run.model_copy(
        update={
            "unscored_criteria": (),
            "routing": Routing.AUTO,
            "criterion_scores": (
                *run.criterion_scores,
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=READABILITY,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=3,
                    score_ratio=1.0,
                    weight=1.0,
                    confidence=1.0,
                    conclusive=True,
                    rationale="読みやすい。",
                ),
            ),
        }
    )

    view = build_result_view(complete, task, None)

    assert view.score_ratio == 1.0
    assert not view.score_withheld
