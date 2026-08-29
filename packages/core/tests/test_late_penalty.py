"""遅延の減点の規則を固定する。

固定したいのは 4 つ。

評価と独立   減点は `GradingRun.score_ratio`（評価）を動かさない。動かすと
             観点の一致度（κ）に事務上の遅れが混ざる。
段で決まる   超過時間が入る最も上の段が当たる。
教員が覆せる 免除できる。覆せない減点は設計原則 P5 に反する。
免除は不同意ではない 免除しても `agreed` は真のまま ── AI の判定に反対した
             のではない（ADR 0010 が Finalization と HumanReview を分けたのと
             同じ理由）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    HumanReview,
    LatePenalty,
    LatePenaltyStep,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    final_score,
    late_penalty_for,
    penalty_crosses_boundary,
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

DUE = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
LADDER = (
    LatePenaltyStep(after_hours=0.0, ratio=0.10),
    LatePenaltyStep(after_hours=24.0, ratio=0.30),
    LatePenaltyStep(after_hours=72.0, ratio=0.50),
)


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CRITERION,
        code="content",
        title="内容",
        description="読んで判断する観点。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="部分的", score_ratio=0.5),
            RubricLevel(level=2, label="達成", descriptor="十分", score_ratio=1.0),
        ),
    )


def _task_version() -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="report_ja",
        statement="## 課題 ##\n\n書きなさい。",
        criteria=(_criterion(),),
        max_score=10.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=DUE,
    )


def _run(level: int = 2, penalty: LatePenalty | None = None) -> GradingRun:
    ratio = {0: 0.0, 1: 0.5, 2: 1.0}[level]
    return GradingRun(
        id=GradingRunId("grn_" + "a" * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        context=GradingContext(
            task_version_id=VERSION,
            subject_profile="report_ja",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "c" * 32),
                criterion_id=CRITERION,
                evaluator_result_id=EvaluatorResultId("evr_" + "d" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=level,
                score_ratio=ratio,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="提出そのものを見た。",
            ),
        ),
        score_ratio=ratio,
        confidence=1.0,
        routing=Routing.AUTO,
        penalty=penalty,
        created_at=DUE,
    )


def _review(*, waived: bool = False, levels: dict | None = None) -> HumanReview:
    return HumanReview(
        id=HumanReviewId("hrv_" + "e" * 32),
        grading_run_id=GradingRunId("grn_" + "a" * 32),
        grader_id=UserId("usr_" + "f" * 32),
        adjusted_levels=levels or {},
        penalty_waived=waived,
        comment="提出物と締切の記録を確認しました。判断の根拠は以上です。",
        reviewed_at=DUE,
    )


# -- 段 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(0.5, 0.10), (23.9, 0.10), (24.1, 0.30), (100.0, 0.50)],
)
def test_the_ladder_picks_the_highest_step_the_delay_reaches(
    hours: float, expected: float
) -> None:
    penalty = late_penalty_for(DUE, DUE + timedelta(hours=hours), LADDER)
    assert penalty is not None
    assert penalty.ratio == expected


def test_an_on_time_submission_is_not_penalised() -> None:
    assert late_penalty_for(DUE, DUE, LADDER) is None
    assert late_penalty_for(DUE, DUE - timedelta(seconds=1), LADDER) is None


def test_without_a_deadline_or_a_rule_nothing_is_deducted() -> None:
    """**規則が無いことと、規則を適用して 0 だったことは違う。**

    どちらも減点は無いが、前者では `LatePenalty` が作られない ── 画面にも
    「遅延の減点」の欄が出ない。出してしまうと、規則を置き忘れたコースで
    「遅延を確認しました」と読める。
    """
    assert late_penalty_for(None, DUE + timedelta(days=3), LADDER) is None
    assert late_penalty_for(DUE, DUE + timedelta(days=3), ()) is None


# -- 評価との独立性 ---------------------------------------------------------


def test_the_penalty_does_not_touch_the_evaluation() -> None:
    penalty = late_penalty_for(DUE, DUE + timedelta(hours=26), LADDER)
    run = _run(level=2, penalty=penalty)

    score = final_score(run, _task_version())
    assert score.evaluation == 1.0, "評価が減点で動いている"
    assert score.penalty_ratio == 0.30
    assert score.final == 0.70
    # 記録された評価そのものも動かない（S7 の習熟度はこちらを読む）。
    assert run.score_ratio == 1.0


def test_the_penalty_cannot_push_the_grade_below_zero() -> None:
    run = _run(level=0, penalty=late_penalty_for(DUE, DUE + timedelta(days=5), LADDER))
    assert final_score(run, _task_version()).final == 0.0


# -- 教員の権限 -------------------------------------------------------------


def test_an_instructor_can_waive_the_penalty() -> None:
    run = _run(level=2, penalty=late_penalty_for(DUE, DUE + timedelta(hours=26), LADDER))
    score = final_score(run, _task_version(), _review(waived=True))
    assert score.penalty_ratio == 0.0
    assert score.final == 1.0
    assert score.waived


def test_waiving_is_not_disagreeing_with_the_ai() -> None:
    """免除しても `agreed` は真。κ を汚さない（ADR 0010 と同じ分け方）。"""
    assert _review(waived=True).agreed


def test_an_adjustment_is_applied_before_the_penalty() -> None:
    """教員が段階を下げたら、減点はその値から引く。"""
    run = _run(level=2, penalty=late_penalty_for(DUE, DUE + timedelta(hours=1), LADDER))
    score = final_score(run, _task_version(), _review(levels={CRITERION: 1}))
    assert score.evaluation == 0.5
    assert score.final == 0.4


# -- 合否境界 ---------------------------------------------------------------


def test_a_penalty_that_flips_the_verdict_is_flagged() -> None:
    """評価では及第、減点で不可 ── 自動で閉じてよい件ではない（P5）。"""
    run = _run(level=1, penalty=late_penalty_for(DUE, DUE + timedelta(hours=1), LADDER))
    score = final_score(run, _task_version())
    assert score.evaluation == 0.5
    assert score.final == 0.4
    assert penalty_crosses_boundary(score, 0.45)


def test_a_penalty_that_does_not_flip_the_verdict_is_not_flagged() -> None:
    run = _run(level=2, penalty=late_penalty_for(DUE, DUE + timedelta(hours=1), LADDER))
    assert not penalty_crosses_boundary(final_score(run, _task_version()), 0.6)


def test_without_a_boundary_nothing_is_flagged() -> None:
    run = _run(level=1, penalty=late_penalty_for(DUE, DUE + timedelta(hours=1), LADDER))
    assert not penalty_crosses_boundary(final_score(run, _task_version()), None)
