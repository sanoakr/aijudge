"""観点ごとに AI 評価器のモデルの「重さ」を選ぶ（2026-11-13）。

既定の gemma4:e4b ではレポートやコメントの説明を読む観点が重すぎた。観点は
重さの名前（`judge_model: large`）だけを持ち、実モデルへの対応は科目プロファイル
（`judge_models`）が持つ。パイプラインが名前を解決して評価器へ渡す。
"""

from __future__ import annotations

import pytest
from test_human_scored_criteria import CORRECTNESS, DIAGRAM, _criterion, _submission, _task_version

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    RubricCriterion,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading import (
    LOCKED_KEYS,
    EvaluatorRegistry,
    GradingPipeline,
    JudgeModel,
    SubjectProfile,
    effective_profile,
)
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

LARGE = JudgeModel(model="qwen3.6:35b-a3b", fallback_model="qwen3.8:27b-mlx", timeout_seconds=600.0)


class _RecordingAi:
    """受け取った要求を覚えておく AI 評価器の代役。"""

    evaluator_id = "rubric_ai_judge"
    kind = EvaluatorKind.AI

    def __init__(self) -> None:
        self.requests: list[EvaluationRequest] = []

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        self.requests.append(request)
        criterion = request.criterion
        assert criterion is not None
        model, _fallback, _ctx = request.model_choice("gemma4:e4b")
        return EvaluationOutcome(
            scores=(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.AI,
                    level=1,
                    score_ratio=1.0,
                    weight=criterion.weight,
                    confidence=1.0,
                    evidence=(
                        Evidence(
                            artifact_id=ArtifactId("art_" + "7" * 32),
                            artifact_content_hash="sha256:x",
                            span=WholeSpan(),
                        ),
                    ),
                    rationale="代役。",
                ),
            ),
            model_id=model,
        )


def _heavy(criterion: RubricCriterion, name: str = "large") -> RubricCriterion:
    return criterion.model_copy(update={"judge_model": name})


def _run(profile: SubjectProfile, *criteria: RubricCriterion):
    ai = _RecordingAi()
    registry = EvaluatorRegistry()
    registry.register(ai)
    task = _task_version(*criteria)
    pipeline = GradingPipeline(registry, profile)
    return ai, pipeline.run(task, _submission(), lambda _artifact: b"x")


def _profile(**kwargs: object) -> SubjectProfile:
    return SubjectProfile(
        name="test_subject",
        ai_evaluators=("rubric_ai_judge",),
        timeout_seconds=120.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_named_weight_reaches_the_evaluator_as_a_model() -> None:
    ai, run = _run(
        _profile(judge_models={"large": LARGE}),
        _criterion(CORRECTNESS, "answer", 0.5, None),
        _heavy(_criterion(DIAGRAM, "comments", 0.5, None)),
    )
    by_code = {request.criterion.code: request for request in ai.requests if request.criterion}
    assert by_code["answer"].judge_model is None
    assert by_code["answer"].model_choice("gemma4:e4b") == ("gemma4:e4b", None, None)
    assert by_code["comments"].judge_model == LARGE
    assert by_code["comments"].model_choice("gemma4:e4b") == (
        "qwen3.6:35b-a3b",
        "qwen3.8:27b-mlx",
        None,
    )
    # 重いモデルは遅いので、重さが決めた上限で呼ぶ（書かなければ科目の値）
    assert by_code["comments"].timeout_seconds == 600.0
    assert by_code["answer"].timeout_seconds == 120.0
    assert len(run.criterion_scores) == 2


def test_each_weight_is_recorded_under_its_own_key() -> None:
    """既定と重いモデルが同じ run で答えても、どちらの記録も消えない（P8）。"""
    _ai, run = _run(
        _profile(judge_models={"large": LARGE}),
        _criterion(CORRECTNESS, "answer", 0.5, None),
        _heavy(_criterion(DIAGRAM, "comments", 0.5, None)),
    )
    assert run.context.model_ids == {
        "rubric_ai_judge": "gemma4:e4b",
        "rubric_ai_judge@large": "qwen3.6:35b-a3b",
    }
    assert run.context.model_params["rubric_ai_judge@large"]["judge_model"]["model"] == (
        "qwen3.6:35b-a3b"
    )


def test_an_unknown_weight_fails_the_criterion_instead_of_using_the_default() -> None:
    """黙って既定の軽いモデルに落とさない。観点は未採点になり人に回る。"""
    ai, run = _run(
        _profile(),
        _criterion(CORRECTNESS, "answer", 0.5, None),
        _heavy(_criterion(DIAGRAM, "comments", 0.5, None)),
    )
    assert [request.criterion.code for request in ai.requests if request.criterion] == ["answer"]
    failed = [r for r in run.evaluator_results if r.status is EvaluatorStatus.FAILED]
    assert len(failed) == 1
    assert "large" in (failed[0].error or "")
    assert {score.criterion_id for score in run.criterion_scores} == {CORRECTNESS}


def test_profile_names_must_have_the_criterion_shape() -> None:
    """モデル名を名前の側に書いたら、プロファイルの読み込みで断る。"""
    with pytest.raises(ValueError, match="judge_models names"):
        _profile(judge_models={"qwen3.6:35b-a3b": LARGE})


def test_a_criterion_cannot_name_a_model_directly() -> None:
    with pytest.raises(ValueError):
        _criterion(CORRECTNESS, "answer", 1.0, None).model_validate(
            {
                **_criterion(CORRECTNESS, "answer", 1.0, None).model_dump(),
                "judge_model": "qwen3.6:35b-a3b",
            }
        )


def test_judge_models_follow_the_template_and_are_not_a_course_override() -> None:
    """重さの対応は運用機の構成で、コースの上書きでは変えない（P7）。"""
    base = _profile(judge_models={"large": LARGE})
    applied = effective_profile(
        base, {"judge_models": {"large": {"model": "somewhere-else"}}, "timeout_seconds": 30.0}
    )
    assert applied.judge_models == {"large": LARGE}
    assert applied.timeout_seconds == 30.0
    assert "judge_models" in LOCKED_KEYS
