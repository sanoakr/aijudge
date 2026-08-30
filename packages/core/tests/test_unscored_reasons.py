"""機械が点を付けなかった「理由」を分けて持つ規則を固定する（Issue #10）。

観点に `CriterionScore` が無い状況は 3 つあり、そこから導く帰結は正反対に
なる。1 つのバケツに混ぜると、**ゲートで打ち切られた学習者の点が上がり**
（重みが再配分されるため）、**人が採点すべき提出が誰も見ないまま閉じる**。

                    重み再配分  総合点   レビュー必須  自動確定
  評価器が落ちた        する      保留        する       止める
  ゲートで打ち切った    しない    出す        しない     止めない
  人が採点する          しない    保留        する       止める

ここのテストは、この表の 4 列がそれぞれ正しい側に付いていることだけを見る。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    aggregate,
    auto_finalizable,
    bulk_finalizable,
    final_score,
    renormalize,
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

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
VERSION = TaskVersionId("tsv_" + "2" * 32)
# 観点 4 つ。重みは Issue #10 の表と同じ 0.3 / 0.3 / 0.2 / 0.2。
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)
C3 = CriterionId("crt_" + "3" * 32)
C4 = CriterionId("crt_" + "4" * 32)


def _score(criterion_id: CriterionId, ratio: float, weight: float) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId("cs_" + criterion_id[-1] * 32),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId("evr_" + "d" * 32),
        kind=EvaluatorKind.DETERMINISTIC,
        level=2 if ratio else 0,
        score_ratio=ratio,
        weight=weight,
        confidence=1.0,
        conclusive=True,
        rationale="テストを実行した。",
    )


def _run(
    scores: tuple[CriterionScore, ...],
    *,
    routing: Routing = Routing.AUTO,
    score_ratio: float = 1.0,
    **reasons: tuple[CriterionId, ...],
) -> GradingRun:
    return GradingRun(
        id=GradingRunId("grn_" + "a" * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        context=GradingContext(
            task_version_id=VERSION,
            subject_profile="cs_intro_c",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=scores,
        score_ratio=score_ratio,
        confidence=1.0,
        routing=routing,
        created_at=NOW,
        **reasons,
    )


# --------------------------------------------------------------------------
# 状態の帰結
# --------------------------------------------------------------------------


def test_a_gated_criterion_is_not_provisional() -> None:
    """ゲートで打ち切った観点は保留ではない ── 総合点を出し、自動確定も止めない。

    0% は確定した結果であって「まだ分からない」ではない。ここを暫定に数えると、
    仕様どおりに働いた打ち切りが、いつまでも点の出ない画面と教員の待ち行列を
    作り続ける。
    """
    run = _run((_score(C1, 0.0, 0.3),), score_ratio=0.0, skipped_criteria=(C2, C3, C4))
    assert not run.is_provisional
    assert auto_finalizable(run, None)


def test_a_criterion_awaiting_a_human_holds_the_grade_open() -> None:
    """人が採点する観点は、人が入れるまで閉じない（設計原則 P5）。"""
    run = _run(
        (_score(C1, 1.0, 0.7),),
        routing=Routing.REVIEW_REQUIRED,
        awaiting_human=(C2,),
    )
    assert run.is_provisional
    assert not auto_finalizable(run, None)


def test_a_criterion_awaiting_a_human_must_be_routed_to_review() -> None:
    with pytest.raises(ValidationError, match="must be routed to review"):
        _run((_score(C1, 1.0, 0.7),), awaiting_human=(C2,))


def test_a_failed_evaluator_still_holds_the_grade_open() -> None:
    """既存の挙動は変えない（ADR 0007 の判断はそのまま）。"""
    with pytest.raises(ValidationError, match="must be routed to review"):
        _run((_score(C1, 1.0, 0.7),), unscored_criteria=(C2,))

    run = _run(
        (_score(C1, 1.0, 0.7),),
        routing=Routing.REVIEW_REQUIRED,
        unscored_criteria=(C2,),
    )
    assert run.is_provisional
    assert not auto_finalizable(run, None)


def test_missing_criteria_gathers_every_reason() -> None:
    """「機械の判定が無い」だけを見たい側（測定・表示）はこれを読む。"""
    run = _run(
        (_score(C1, 1.0, 0.4),),
        routing=Routing.REVIEW_REQUIRED,
        unscored_criteria=(C2,),
        skipped_criteria=(C3,),
        awaiting_human=(C4,),
    )
    assert set(run.missing_criteria) == {C2, C3, C4}


# --------------------------------------------------------------------------
# 理由は 1 つだけ
# --------------------------------------------------------------------------


def test_a_criterion_cannot_carry_two_reasons() -> None:
    """2 つ付くと、どちらの帰結を当てるかが読む側次第になる。"""
    with pytest.raises(ValidationError, match="more than one reason"):
        _run(
            (_score(C1, 1.0, 0.7),),
            routing=Routing.REVIEW_REQUIRED,
            skipped_criteria=(C2,),
            awaiting_human=(C2,),
        )


def test_a_scored_criterion_cannot_also_be_missing() -> None:
    """点が付いている観点を未採点として数えると、重みが二重に効く。"""
    with pytest.raises(ValidationError, match="has a score but is listed as"):
        _run(
            (_score(C1, 1.0, 1.0),),
            routing=Routing.REVIEW_REQUIRED,
            unscored_criteria=(C1,),
        )


# --------------------------------------------------------------------------
# 点の作り方
# --------------------------------------------------------------------------


def test_a_gate_counts_the_cut_criteria_as_zero_not_as_absent() -> None:
    """Issue #10 の表そのもの。**打ち切りで点が上がってはならない。**

    観点 4 つ（0.3 / 0.3 / 0.2 / 0.2）で 2 つめが 0% になり、3・4 を打ち切った
    とき、意図された点は 30%。打ち切りを「採点できなかった」として重みを
    再配分すると 50% になり、AND の意味が消える。
    """
    scored = (_score(C1, 1.0, 0.3), _score(C2, 0.0, 0.3))

    total, _ = aggregate(scored, zero_weight=0.4)
    assert total == pytest.approx(0.3)

    # 比較。再配分するとこうなる ── これがバグの形。
    renormalized, _ = aggregate(renormalize(scored))
    assert renormalized == pytest.approx(0.5)


def test_aggregate_still_demands_that_every_weight_is_accounted_for() -> None:
    """0% として数える重みを渡し忘れたら落とす。黙って比例配分し直さない。"""
    with pytest.raises(ValueError, match=r"must sum to 1\.0"):
        aggregate((_score(C1, 1.0, 0.3), _score(C2, 0.0, 0.3)))


def test_aggregate_accepts_a_run_where_the_machine_scored_nothing() -> None:
    """全観点が人採点の課題（#7）。スコアが 0 件でも重みが揃えば点は作れる。"""
    total, confidence = aggregate((), zero_weight=1.0)
    assert total == pytest.approx(0.0)
    assert confidence == 1.0


# --------------------------------------------------------------------------
# 人が採点する観点（Issue #7）
# --------------------------------------------------------------------------


def _rubric(*criteria: tuple[CriterionId, str, float]) -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="## 課題 ##\n\n出しなさい。",
        criteria=tuple(
            RubricCriterion(
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
            for criterion_id, code, weight in criteria
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )


def _review(levels: dict[CriterionId, int]) -> HumanReview:
    return HumanReview(
        id=HumanReviewId("hrv_" + "e" * 32),
        grading_run_id=GradingRunId("grn_" + "a" * 32),
        grader_id=UserId("usr_" + "f" * 32),
        adjusted_levels=levels,
        comment="図を見て判断しました。根拠は以上です。",
        reviewed_at=NOW,
    )


def test_a_level_entered_for_a_criterion_the_machine_never_scored_counts() -> None:
    """**人が入れた段階が総合点に乗る**（Issue #7 のバグ 1）。

    以前は `run.criterion_scores` を回していたので、機械が一度も採点しなかった
    観点への教員の入力は `HumanReview` に残るのに点には出なかった。評価器を
    割り当てない観点は定義上いつもこれに当たるので、人手の採点が成績に
    乗らないままだった。
    """
    task = _rubric((C1, "correctness", 0.6), (C2, "diagram", 0.4))
    run = _run(
        (_score(C1, 1.0, 0.6),),
        routing=Routing.REVIEW_REQUIRED,
        score_ratio=0.6,
        awaiting_human=(C2,),
    )

    settled = final_score(run, task, _review({C2: 2}))
    assert settled.evaluation == pytest.approx(1.0)

    # 教員が「未達」と入れれば、その観点の重みぶんだけ下がる。
    failed = final_score(run, task, _review({C2: 0}))
    assert failed.evaluation == pytest.approx(0.6)


def test_bulk_finalization_will_not_close_a_criterion_a_human_must_score() -> None:
    """一括確定は「読まずに責任を取る」操作だが、**署名する対象が無い**。

    まとめて閉じれば、その観点の重みを除いた暫定の点がそのまま成績になる。
    評価器が落ちた観点（決定的評価の結果は出ている）は従来どおり通す。
    """
    awaiting = _run(
        (_score(C1, 1.0, 0.6),),
        routing=Routing.REVIEW_REQUIRED,
        score_ratio=0.6,
        awaiting_human=(C2,),
    )
    assert not bulk_finalizable(awaiting, None)

    broken = _run(
        (_score(C1, 1.0, 1.0),),
        routing=Routing.REVIEW_REQUIRED,
        unscored_criteria=(C2,),
    )
    assert bulk_finalizable(broken, None)
