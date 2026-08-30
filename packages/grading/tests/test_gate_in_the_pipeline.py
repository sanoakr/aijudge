"""AND ゲートがパイプラインで効くことを固定する（Issue #5）。

Issue #10 の表そのものを、実際の採点で確かめる。

  観点 4 つ（0.3 / 0.3 / 0.2 / 0.2）、2 つめが 0%、3・4 を打ち切り
    意図           30%
    再配分すると   50%   ← 打ち切られた学習者の点が上がる

**呼ばないこと自体が目的である。** 打ち切りは「動かないコードの読みやすさを
評価しても意味が無い」という順序関係の表明で、LLM の呼び出しがそのまま費用と
待ち時間になる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    Aggregation,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    CharSpan,
    CriterionScore,
    EvaluatorKind,
    Evidence,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import EvaluatorRegistry, GradingPipeline, SubjectProfile
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)
C3 = CriterionId("crt_" + "3" * 32)
C4 = CriterionId("crt_" + "4" * 32)


def _criterion(criterion_id: CriterionId, code: str, weight: float) -> RubricCriterion:
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=code,
        description=f"{code} を見る観点。",
        weight=weight,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="十分", score_ratio=1.0),
        ),
    )


def _task_version() -> TaskVersion:
    """評価順は宣言の並び。上から「動く」「正しい」「読める」「速い」。"""
    return TaskVersion(
        id=TaskVersionId(new_id("tsv")),
        task_id=TaskId(new_id("tsk")),
        version=1,
        subject_profile="test_subject",
        statement="## 課題 ##\n\n書きなさい。",
        criteria=(
            _criterion(C1, "runs", 0.3),
            _criterion(C2, "correct", 0.3),
            _criterion(C3, "readable", 0.2),
            _criterion(C4, "fast", 0.2),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId(new_id("usr"))),
        created_at=NOW,
    )


def _submission() -> Submission:
    submission_id = SubmissionId(new_id("sub"))
    return Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(
            Artifact(
                id=ArtifactId(new_id("art")),
                submission_id=submission_id,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.CODE,
                filename="answer.c",
                storage_key=f"memory://{submission_id}",
                content_hash="sha256:x",
                byte_size=1,
                created_at=NOW,
            ),
        ),
        submitted_at=NOW,
        created_at=NOW,
    )


class _Tests:
    """決定的評価器。上 2 つの観点だけを見て、2 つめを落とす。"""

    evaluator_id = "tests"
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        ratios = {C1: 1.0, C2: 0.0}
        return EvaluationOutcome(
            scores=tuple(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=1 if ratios[criterion.id] else 0,
                    score_ratio=ratios[criterion.id],
                    weight=criterion.weight,
                    confidence=1.0,
                    conclusive=True,
                    rationale="テストを実行した。",
                )
                for criterion in request.task_version.criteria
                if criterion.id in ratios
            )
        )


class _Judge:
    """AI 評価器。**何を聞かれたかを覚える。**"""

    evaluator_id = "judge"
    kind = EvaluatorKind.AI

    def __init__(self) -> None:
        self.asked: list[str] = []

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion
        assert criterion is not None
        self.asked.append(criterion.code)
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
                            artifact_id=ArtifactId(new_id("art")),
                            artifact_content_hash="sha256:x",
                            span=CharSpan(start=0, end=4),
                        ),
                    ),
                    rationale="読んで判断した。",
                ),
            )
        )


def _run(aggregation: Aggregation):
    judge = _Judge()
    registry = EvaluatorRegistry()
    registry.register(_Tests())
    registry.register(judge)
    profile = SubjectProfile(
        name="test_subject", deterministic=("tests",), ai_evaluators=("judge",)
    )
    pipeline = GradingPipeline(registry, profile)
    run = pipeline.run(
        _task_version(),
        _submission(),
        lambda _artifact: b"int main(){}",
        aggregation=aggregation,
    )
    return run, judge


def test_the_gate_stops_asking_once_a_criterion_scores_zero() -> None:
    """**呼ばないこと自体が目的。** 打ち切った観点に LLM の費用は掛からない。"""
    run, judge = _run(Aggregation.AND)

    assert judge.asked == []
    assert run.skipped_criteria == (C3, C4)


def test_the_cut_criteria_count_as_zero_at_their_own_weight() -> None:
    """意図は 30%。再配分すれば 50% になり、AND の意味が消える。"""
    run, _ = _run(Aggregation.AND)

    assert run.score_ratio == pytest.approx(0.3)


def test_a_cut_does_not_send_the_submission_to_review() -> None:
    """打ち切りは仕様どおりの結果で、人が見るべき異常ではない（ADR 0015）。"""
    run, _ = _run(Aggregation.AND)

    assert not run.is_provisional
    assert run.routing is Routing.AUTO
    assert run.unscored_criteria == ()


def test_or_keeps_asking_and_keeps_the_old_behaviour() -> None:
    """**宣言するまで何も変わらない。** 既定は OR。"""
    run, judge = _run(Aggregation.OR)

    assert judge.asked == ["readable", "fast"]
    assert run.skipped_criteria == ()
    assert run.score_ratio == pytest.approx(0.7)
