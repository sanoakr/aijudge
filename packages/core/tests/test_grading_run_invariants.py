"""`GradingRun` の不変条件と `resolve_conflicts` を固定する（#684）。

`GradingRun` は保存したら書き換えない（P8）ので、作る時点で拒むしかない。
ここで守っているのは 3 つで、どれを `if False:` にしてもスイートは通っていた。

- AI の点は根拠を持つ（P4）。根拠の無い AI の判定が run として残ると、学習者に
  「どこを見てそう判断したか」を返せない。既存の `_score` ヘルパは AI の点に必ず
  根拠を付けるので、付けない側は一度も作られていなかった
- 点は、その run に記録した評価器の出力を指す。指さない点は再現できない
- 点も未採点の理由も無い run は、ただの空の記録である

`resolve_conflicts` は、同じ観点に決定的評価が 2 つ付いたプロファイルの設定ミスを
例外にする。黙って先頭を採ると、どちらで採点されたかが設定の並び順で決まる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aijudge_core import (
    CharSpan,
    CriterionScore,
    EvaluatorKind,
    EvaluatorResult,
    Evidence,
    GradingContext,
    GradingRun,
    Routing,
    resolve_conflicts,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)
DETERMINISTIC_RESULT = EvaluatorResultId("evr_" + "d" * 32)
AI_RESULT = EvaluatorResultId("evr_" + "a" * 32)
EVIDENCE = Evidence(
    artifact_id=ArtifactId("art_" + "5" * 32),
    artifact_content_hash="sha256:x",
    span=CharSpan(start=0, end=10),
)


def _score(
    criterion_id: CriterionId,
    *,
    kind: EvaluatorKind,
    result_id: EvaluatorResultId,
    evidence: tuple[Evidence, ...] = (),
    suffix: str = "1",
) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId("cs_" + suffix * 32),
        criterion_id=criterion_id,
        evaluator_result_id=result_id,
        kind=kind,
        level=2,
        score_ratio=1.0,
        weight=1.0,
        confidence=0.9,
        conclusive=kind is EvaluatorKind.DETERMINISTIC,
        evidence=evidence,
        rationale="判定の理由。",
    )


def _result(result_id: EvaluatorResultId, kind: EvaluatorKind) -> EvaluatorResult:
    return EvaluatorResult(id=result_id, evaluator_id=f"{kind.value}_eval", kind=kind)


def _run(
    scores: tuple[CriterionScore, ...],
    *,
    results: tuple[EvaluatorResult, ...] = (),
    routing: Routing = Routing.AUTO,
    **reasons: tuple[CriterionId, ...],
) -> GradingRun:
    return GradingRun(
        id=GradingRunId("grn_" + "a" * 32),
        submission_id=SubmissionId("sub_" + "b" * 32),
        context=GradingContext(
            task_version_id=TaskVersionId("tsv_" + "2" * 32),
            subject_profile="cs_lang_c_intro",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        evaluator_results=results,
        criterion_scores=scores,
        score_ratio=1.0,
        confidence=0.9,
        routing=routing,
        created_at=NOW,
        **reasons,
    )


# --------------------------------------------------------------------------
# AI の点は根拠を持つ（P4）
# --------------------------------------------------------------------------


def test_an_ai_score_without_evidence_is_refused() -> None:
    bare = _score(C1, kind=EvaluatorKind.AI, result_id=AI_RESULT)
    with pytest.raises(ValidationError, match="must carry evidence"):
        _run((bare,), results=(_result(AI_RESULT, EvaluatorKind.AI),))


def test_an_ai_score_with_evidence_is_accepted() -> None:
    """拒む側だけでは、検査が何でも拒む形に化けても気づけない。"""
    evidenced = _score(C1, kind=EvaluatorKind.AI, result_id=AI_RESULT, evidence=(EVIDENCE,))
    run = _run((evidenced,), results=(_result(AI_RESULT, EvaluatorKind.AI),))
    assert run.criterion_scores == (evidenced,)


def test_a_deterministic_score_needs_no_evidence() -> None:
    """決定的評価の根拠はテストの実行結果そのもので、本文のスパンは求めない。"""
    score = _score(C1, kind=EvaluatorKind.DETERMINISTIC, result_id=DETERMINISTIC_RESULT)
    run = _run((score,), results=(_result(DETERMINISTIC_RESULT, EvaluatorKind.DETERMINISTIC),))
    assert run.criterion_scores == (score,)


# --------------------------------------------------------------------------
# 点は記録した評価器の出力を指す
# --------------------------------------------------------------------------


def test_a_score_pointing_at_an_unrecorded_evaluator_result_is_refused() -> None:
    stray = _score(C1, kind=EvaluatorKind.DETERMINISTIC, result_id=DETERMINISTIC_RESULT)
    with pytest.raises(ValidationError, match="unknown EvaluatorResult"):
        _run((stray,), results=(_result(AI_RESULT, EvaluatorKind.AI),))


# --------------------------------------------------------------------------
# 点も理由も無い run
# --------------------------------------------------------------------------


def test_a_run_with_no_score_and_no_reason_is_refused() -> None:
    with pytest.raises(ValidationError, match="must say why"):
        _run(())


def test_a_run_with_no_score_but_a_reason_is_accepted() -> None:
    """理由があれば空でも通す ── 全観点を人が採点する課題や、AI 段階へ渡す前の run（#80）。"""
    awaiting = _run((), routing=Routing.REVIEW_REQUIRED, awaiting_human=(C1,))
    assert awaiting.is_provisional

    unscored = _run((), routing=Routing.REVIEW_REQUIRED, unscored_criteria=(C1,))
    assert unscored.is_provisional


# --------------------------------------------------------------------------
# resolve_conflicts
# --------------------------------------------------------------------------


def test_two_deterministic_scores_on_one_criterion_are_a_profile_error() -> None:
    first = _score(C1, kind=EvaluatorKind.DETERMINISTIC, result_id=DETERMINISTIC_RESULT)
    second = _score(
        C1,
        kind=EvaluatorKind.DETERMINISTIC,
        result_id=EvaluatorResultId("evr_" + "e" * 32),
        suffix="2",
    )
    with pytest.raises(ValueError, match="multiple deterministic"):
        resolve_conflicts((first, second))


def test_one_deterministic_score_per_criterion_is_not_a_conflict() -> None:
    """観点が違えば、決定的評価が 2 つあっても衝突ではない。"""
    first = _score(C1, kind=EvaluatorKind.DETERMINISTIC, result_id=DETERMINISTIC_RESULT)
    second = _score(
        C2, kind=EvaluatorKind.DETERMINISTIC, result_id=DETERMINISTIC_RESULT, suffix="2"
    )
    assert resolve_conflicts((first, second)) == (first, second)
