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
    Routing,
    aggregate,
    auto_finalizable,
    renormalize,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
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
