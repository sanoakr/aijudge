"""確定していない決定的評価の観点のために、AI 段階のジョブを積まない。

text_pattern_check は線引きの決まらない観点を確定させずにレビューへ回す
（2026-11-13・survey）。AI 段階はその観点を呼ばない（評価器の割り当てが
違う）ので、積んでも何も変わらないジョブがキューに溜まるだけになる。
"""

from __future__ import annotations

from test_human_scored_criteria import (
    CORRECTNESS,
    DIAGRAM,
    _criterion,
    _submission,
    _task_version,
)

from aijudge_core import CriterionScore, EvaluatorKind, GradingPhase, new_id
from aijudge_core.ids import CriterionScoreId, EvaluatorResultId
from aijudge_grading import EvaluatorRegistry, GradingPipeline, SubjectProfile
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest


class _Undecided:
    """観点を確定させずに返す決定的評価器（確信度は低い）。"""

    evaluator_id = "undecided_check"
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        scores = [
            CriterionScore(
                id=CriterionScoreId(new_id("cs")),
                criterion_id=criterion.id,
                evaluator_result_id=EvaluatorResultId(new_id("evr")),
                kind=EvaluatorKind.DETERMINISTIC,
                level=0,
                score_ratio=0.0,
                weight=criterion.weight,
                confidence=0.5,
                conclusive=False,
                rationale="決まらない。",
            )
            for criterion in request.task_version.criteria
            if criterion.evaluator_id == self.evaluator_id
        ]
        return EvaluationOutcome(scores=tuple(scores))


class _Ai:
    """AI 段階の評価器の代役。この検査では呼ばれない。"""

    evaluator_id = "rubric_ai_judge"
    kind = EvaluatorKind.AI

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:  # pragma: no cover
        raise AssertionError("has_ai_work は評価器を呼ばない")


def _pipeline() -> GradingPipeline:
    registry = EvaluatorRegistry()
    registry.register(_Undecided())
    registry.register(_Ai())
    profile = SubjectProfile(
        name="test_subject",
        deterministic=("undecided_check",),
        ai_evaluators=("rubric_ai_judge",),
    )
    return GradingPipeline(registry, profile)


def _base(pipeline: GradingPipeline, task):
    return pipeline.run(
        task, _submission(), lambda _artifact: b"x", phase=GradingPhase.DETERMINISTIC
    )


def test_an_undecided_deterministic_criterion_does_not_queue_ai_work() -> None:
    task = _task_version(_criterion(CORRECTNESS, "answer", 1.0, "undecided_check"))
    pipeline = _pipeline()
    assert pipeline.has_ai_work(task, _base(pipeline, task)) is False


def test_a_criterion_assigned_to_an_ai_evaluator_still_queues_ai_work() -> None:
    task = _task_version(
        _criterion(CORRECTNESS, "answer", 0.5, "undecided_check"),
        _criterion(DIAGRAM, "quality", 0.5, "rubric_ai_judge"),
    )
    pipeline = _pipeline()
    assert pipeline.has_ai_work(task, _base(pipeline, task)) is True


def test_a_criterion_open_to_any_ai_evaluator_still_queues_ai_work() -> None:
    """割り当てが空の観点は「どの AI 評価器からも対象」（ADR 0015）。"""
    task = _task_version(
        _criterion(CORRECTNESS, "answer", 0.5, "undecided_check"),
        _criterion(DIAGRAM, "quality", 0.5, None),
    )
    pipeline = _pipeline()
    assert pipeline.has_ai_work(task, _base(pipeline, task)) is True
