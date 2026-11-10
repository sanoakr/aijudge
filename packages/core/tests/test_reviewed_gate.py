"""教員の段階で AND の打ち切りを判定し直す（#675）。

固定したいのは 3 つ。

同じ規則          `reviewed_gate` は `gate_skipped` と同じ規則で、教員の段階を見る。
打ち切りは 0%     確定の点では、打ち切った観点に機械の点が残っていても数えない。
段階は付かない    打ち切った観点に段階を付けた確認は作れない（点が上がった原因）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    Aggregation,
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
    final_score,
    new_id,
    reviewed_gate,
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

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)
C3 = CriterionId("crt_" + "3" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "4" * 32)
COMMENT = "上位の観点を確認しました。動かないので以降は評価しません。"


def _criterion(criterion_id: CriterionId, code: str, weight: float) -> RubricCriterion:
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=code,
        description=f"{code} を見る観点。",
        weight=weight,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="半分", score_ratio=0.5),
            RubricLevel(level=2, label="達成", descriptor="十分", score_ratio=1.0),
        ),
    )


CRITERIA = (
    _criterion(C1, "runs", 0.5),
    _criterion(C2, "correct", 0.3),
    _criterion(C3, "readable", 0.2),
)


def _task() -> TaskVersion:
    return TaskVersion(
        id=TASK_VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="問題文",
        criteria=CRITERIA,
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )


def _score(criterion: RubricCriterion, level: int) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId(new_id("cs")),
        criterion_id=criterion.id,
        evaluator_result_id=EvaluatorResultId(new_id("evr")),
        kind=EvaluatorKind.DETERMINISTIC,
        level=level,
        score_ratio=criterion.level_for(level).score_ratio,
        weight=criterion.weight,
        confidence=1.0,
        conclusive=True,
        rationale="テストを実行した。",
    )


def _run(levels: dict[CriterionId, int], *, skipped: tuple[CriterionId, ...] = ()) -> GradingRun:
    scores = tuple(_score(c, levels[c.id]) for c in CRITERIA if c.id in levels)
    ratio = sum(s.score_ratio * s.weight for s in scores)
    return GradingRun(
        id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        context=GradingContext(
            task_version_id=TASK_VERSION,
            subject_profile="cs_lang_c_intro",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=scores,
        score_ratio=ratio,
        confidence=1.0,
        routing=Routing.AUTO,
        skipped_criteria=skipped,
        created_at=NOW,
    )


def _review(
    run: GradingRun,
    adjusted: dict[CriterionId, int],
    gated: tuple[CriterionId, ...] = (),
) -> HumanReview:
    return HumanReview(
        id=HumanReviewId(new_id("hrv")),
        grading_run_id=run.id,
        grader_id=UserId("usr_" + "7" * 32),
        adjusted_levels=adjusted,
        gated_criteria=gated,
        comment=COMMENT,
        reviewed_at=NOW,
    )


def test_or_never_cuts_anything() -> None:
    """**宣言するまで変わらない。** OR では教員の段階が 0% でも何も切らない。"""
    assert reviewed_gate(CRITERIA, {C1: 0, C2: 2, C3: 2}, Aggregation.OR) == ()


def test_and_cuts_everything_after_the_instructors_first_zero() -> None:
    assert reviewed_gate(CRITERIA, {C1: 0}, Aggregation.AND) == (C2, C3)
    assert reviewed_gate(CRITERIA, {C1: 1, C2: 0}, Aggregation.AND) == (C3,)


def test_and_stops_where_a_level_is_missing() -> None:
    """段階の無い観点で止める ── 欠けは打ち切りではなく、確定の側が断る欠け。"""
    assert reviewed_gate(CRITERIA, {C2: 0}, Aggregation.AND) == ()


def test_a_kept_gate_finalizes_at_zero() -> None:
    """**#675 の事例。** 1 つめが 0% のまま、2 つめ以降は打ち切りで確定する。"""
    run = _run({C1: 0}, skipped=(C2, C3))
    review = _review(run, {}, gated=(C2, C3))

    assert final_score(run, _task(), review).evaluation == 0.0


def test_lowering_the_top_criterion_cuts_the_machine_scores_below_it() -> None:
    """教員が上位を 0% に下げたら、機械が下位に付けた点はもう数えない。"""
    run = _run({C1: 2, C2: 2, C3: 2})
    review = _review(run, {C1: 0}, gated=(C2, C3))

    assert final_score(run, _task(), review).evaluation == 0.0


def test_lifting_the_top_criterion_counts_the_levels_given_below_it() -> None:
    """上位を 0% から引き上げたら打ち切りは消え、入れた段階を重みどおり数える。"""
    run = _run({C1: 0}, skipped=(C2, C3))
    review = _review(run, {C1: 2, C2: 1, C3: 2})

    assert final_score(run, _task(), review).evaluation == pytest.approx(0.5 + 0.15 + 0.2)


def test_a_partial_gate_counts_the_rest_at_rubric_weight() -> None:
    """途中で切れたら、切れる前の観点だけを**ルーブリックの重み**で数える。"""
    run = _run({C1: 2, C2: 2, C3: 2})
    review = _review(run, {C2: 0}, gated=(C3,))

    assert final_score(run, _task(), review).evaluation == pytest.approx(0.5)


def test_a_gated_criterion_cannot_also_carry_a_level() -> None:
    """両方あると、0% と段階のどちらが成績かが読む側次第になる。"""
    run = _run({C1: 0}, skipped=(C2, C3))
    with pytest.raises(ValueError, match="gated but also carry a level"):
        _review(run, {C2: 2}, gated=(C2, C3))


def test_a_review_written_before_the_field_existed_reads_unchanged() -> None:
    """既存の記録は `gated_criteria` を持たない。既定が空なので意味は変わらない。"""
    run = _run({C1: 2, C2: 2, C3: 2})
    review = HumanReview.model_validate(
        {
            "id": new_id("hrv"),
            "grading_run_id": run.id,
            "grader_id": "usr_" + "7" * 32,
            "adjusted_levels": {C2: 1},
            "comment": COMMENT,
            "reviewed_at": NOW,
        }
    )

    assert review.gated_criteria == ()
    assert final_score(run, _task(), review).evaluation == pytest.approx(0.5 + 0.15 + 0.2)
