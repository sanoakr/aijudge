"""AI 段階のジョブを積まない場合を表で固定する（Issue #685）。

`has_ai_work` の真の側は `apps/grader` の worker のテストが見ているが、偽を
返すべき 3 つの条件（AI 評価器が無い・残りが打ち切り・残りが人採点だけ）は
誰も見ていなかった。壊れても AI 段階の run の中では評価器は呼ばれないので
費用は増えないが、遅い列に何もしないジョブが並ぶ（ADR 0011）。

決定的段階の結果（base）は手で組む。`has_ai_work` が読むのは base の点と
打ち切りの記録だけなので、パイプラインを走らせるより条件を 1 つずつ置ける。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_human_scored_criteria import CORRECTNESS, DIAGRAM, _criterion, _task_version

from aijudge_core import (
    HUMAN_SCORED,
    Aggregation,
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Routing,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
)
from aijudge_grading import EvaluatorRegistry, GradingPipeline, SubjectProfile
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


class _Unused:
    """登録だけする評価器の代役。`has_ai_work` は評価器を呼ばない。"""

    def __init__(self, evaluator_id: str, kind: EvaluatorKind) -> None:
        self.evaluator_id = evaluator_id
        self.kind = kind

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:  # pragma: no cover
        raise AssertionError("has_ai_work は評価器を呼ばない")


def _pipeline(*, ai: bool = True) -> GradingPipeline:
    registry = EvaluatorRegistry()
    registry.register(_Unused("tests", EvaluatorKind.DETERMINISTIC))
    registry.register(_Unused("judge", EvaluatorKind.AI))
    profile = SubjectProfile(
        name="test_subject",
        deterministic=("tests",),
        ai_evaluators=("judge",) if ai else (),
    )
    return GradingPipeline(registry, profile)


def _tests_score(criterion_id: CriterionId, ratio: float) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId(new_id("cs")),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId(new_id("evr")),
        kind=EvaluatorKind.DETERMINISTIC,
        level=1 if ratio else 0,
        score_ratio=ratio,
        weight=0.5,
        confidence=1.0,
        conclusive=True,
        rationale="テストを実行した。",
    )


def _base(
    task: TaskVersion,
    scores: tuple[CriterionScore, ...],
    **reasons: tuple[CriterionId, ...],
) -> GradingRun:
    provisional = bool(reasons.get("unscored_criteria") or reasons.get("awaiting_human"))
    return GradingRun(
        id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        context=GradingContext(
            task_version_id=task.id,
            subject_profile="test_subject",
            rubric_version="1",
            input_hash="sha256:x",
            pipeline_version="1",
        ),
        criterion_scores=scores,
        score_ratio=0.5,
        confidence=1.0,
        routing=Routing.REVIEW_REQUIRED if provisional else Routing.AUTO,
        created_at=NOW,
        **reasons,
    )


def _two(diagram_evaluator: str | None) -> TaskVersion:
    """上が決定的評価の「correctness」、下が「diagram」。"""
    return _task_version(
        _criterion(CORRECTNESS, "correctness", 0.5, "tests"),
        _criterion(DIAGRAM, "diagram", 0.5, diagram_evaluator),
    )


def test_no_ai_evaluator_in_the_profile_queues_nothing() -> None:
    """S6 停止時の劣化動作を含む。AI の観点が未採点で残っていても積まない。"""
    task = _two(None)
    base = _base(task, (_tests_score(CORRECTNESS, 1.0),), unscored_criteria=(DIAGRAM,))
    assert _pipeline(ai=False).has_ai_work(task, base) is False


def test_every_criterion_settled_queues_nothing() -> None:
    task = _two("tests")
    base = _base(task, (_tests_score(CORRECTNESS, 1.0), _tests_score(DIAGRAM, 1.0)))
    assert _pipeline().has_ai_work(task, base) is False


def test_a_remainder_already_recorded_as_cut_queues_nothing() -> None:
    """base が打ち切りを記録していれば、ゲートを計算し直さなくても積まない。

    集約を OR で渡し、`gate_skipped` の再計算が何も切らない状況にして、
    `base.skipped_criteria` の経路だけを確かめる。
    """
    task = _two(None)
    base = _base(task, (_tests_score(CORRECTNESS, 0.0),), skipped_criteria=(DIAGRAM,))
    assert _pipeline().has_ai_work(task, base, aggregation=Aggregation.OR) is False


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [(Aggregation.AND, False), (Aggregation.OR, True)],
)
def test_a_remainder_the_gate_would_cut_queues_nothing(
    aggregation: Aggregation, expected: bool
) -> None:
    """AND で上が 0% なら、記録が無くても残りは打ち切り。**OR では積む**（対照）。

    打ち切りは LLM を呼ばないためにある。ここで積めば意味が消える。
    """
    task = _two(None)
    base = _base(task, (_tests_score(CORRECTNESS, 0.0),), unscored_criteria=(DIAGRAM,))
    assert _pipeline().has_ai_work(task, base, aggregation=aggregation) is expected


def test_a_remainder_scored_by_a_human_queues_nothing() -> None:
    """人が採点する観点は AI に渡さない（ADR 0015）。"""
    task = _two(HUMAN_SCORED)
    base = _base(task, (_tests_score(CORRECTNESS, 1.0),), awaiting_human=(DIAGRAM,))
    assert _pipeline().has_ai_work(task, base) is False


@pytest.mark.parametrize("diagram_evaluator", ["judge", None])
def test_one_ai_criterion_left_open_queues_work(diagram_evaluator: str | None) -> None:
    """対照。AI の担当（明示か、割り当てが空か）が 1 つ残れば積む。"""
    task = _two(diagram_evaluator)
    base = _base(task, (_tests_score(CORRECTNESS, 1.0),), unscored_criteria=(DIAGRAM,))
    assert _pipeline().has_ai_work(task, base) is True
