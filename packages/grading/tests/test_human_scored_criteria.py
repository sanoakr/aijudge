"""評価器を割り当てない観点（人が採点する）の規則を固定する（Issue #7）。

固定したいのは 4 つ。

誰にも渡さない   決定的評価器にも AI 評価器にも問い合わせない。**空とは別の
                 状態である** ── 空は「どの AI 評価器からも対象」を意味する。
理由が残る       `awaiting_human` に入る。`unscored_criteria`（評価器が落ちた）
                 ではない。帰結が違う（ADR 0015）。
点が膨らまない   その観点の重みは 0% として数える。比例配分に混ぜると、観点が
                 最初から無かったのと同じ点になる。
全部でも通る     全観点が人採点の課題（画像提出など）でも採点が成立する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    HUMAN_SCORED,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    CriterionScore,
    EvaluatorKind,
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
CORRECTNESS = CriterionId("crt_" + "1" * 32)
DIAGRAM = CriterionId("crt_" + "2" * 32)


def _levels() -> tuple[RubricLevel, ...]:
    return (
        RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
        RubricLevel(level=1, label="達成", descriptor="十分", score_ratio=1.0),
    )


def _criterion(
    criterion_id: CriterionId, code: str, weight: float, evaluator: str | None
) -> RubricCriterion:
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=code,
        description=f"{code} を見る観点。",
        weight=weight,
        levels=_levels(),
        evaluator_id=evaluator,
    )


def _task_version(*criteria: RubricCriterion) -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId(new_id("tsv")),
        task_id=TaskId(new_id("tsk")),
        version=1,
        subject_profile="test_subject",
        statement="## 課題 ##\n\n出しなさい。",
        criteria=criteria,
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


class _Marker:
    """求められた観点をすべて満点にする評価器。**何を求められたかを覚える。**"""

    def __init__(self, evaluator_id: str, kind: EvaluatorKind) -> None:
        self.evaluator_id = evaluator_id
        self.kind = kind
        self.asked: list[str] = []

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criteria = (
            (request.criterion,) if request.criterion is not None else request.task_version.criteria
        )
        scores = []
        for criterion in criteria:
            self.asked.append(criterion.code)
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=self.kind,
                    level=1,
                    score_ratio=1.0,
                    weight=criterion.weight,
                    confidence=1.0,
                    conclusive=self.kind is EvaluatorKind.DETERMINISTIC,
                    rationale="満たしている。",
                )
            )
        return EvaluationOutcome(scores=tuple(scores))


def _pipeline(*, deterministic: bool = True) -> tuple[GradingPipeline, _Marker]:
    marker = _Marker("marker", EvaluatorKind.DETERMINISTIC)
    registry = EvaluatorRegistry()
    registry.register(marker)
    profile = SubjectProfile(
        name="test_subject",
        deterministic=("marker",) if deterministic else (),
    )
    return GradingPipeline(registry, profile), marker


def _run(task_version: TaskVersion, *, deterministic: bool = True):
    pipeline, marker = _pipeline(deterministic=deterministic)
    submission = _submission()
    run = pipeline.run(task_version, submission, lambda _artifact: b"int main(){}")
    return run, marker


def test_a_human_scored_criterion_is_not_offered_to_any_evaluator() -> None:
    """**空とは別の状態である。** 空は「どの AI 評価器からも対象」を意味する。"""
    task = _task_version(
        _criterion(CORRECTNESS, "correctness", 0.6, None),
        _criterion(DIAGRAM, "diagram", 0.4, HUMAN_SCORED),
    )
    run, marker = _run(task)

    # この決定的評価器は渡された課題の全観点に点を付けて返す。**それでも
    # 人採点の観点は採らない** ── 宣言は評価器の都合で覆らない。
    assert "diagram" in marker.asked
    assert {score.criterion_id for score in run.criterion_scores} == {CORRECTNESS}


def test_a_human_scored_criterion_is_recorded_as_awaiting_a_human() -> None:
    """理由を分ける。`unscored_criteria` に入れると帰結が変わる（ADR 0015）。"""
    task = _task_version(
        _criterion(CORRECTNESS, "correctness", 0.6, None),
        _criterion(DIAGRAM, "diagram", 0.4, HUMAN_SCORED),
    )
    run, _ = _run(task)

    assert run.awaiting_human == (DIAGRAM,)
    assert run.unscored_criteria == ()
    assert run.is_provisional
    assert run.routing is Routing.REVIEW_REQUIRED


def test_the_weight_of_a_human_scored_criterion_is_not_handed_to_the_others() -> None:
    """**0% として重みどおり数える。** 比例配分すると満点になってしまう。"""
    task = _task_version(
        _criterion(CORRECTNESS, "correctness", 0.6, None),
        _criterion(DIAGRAM, "diagram", 0.4, HUMAN_SCORED),
    )
    run, _ = _run(task)

    assert run.score_ratio == pytest.approx(0.6)


def test_a_task_scored_entirely_by_a_human_still_grades() -> None:
    """画像提出の課題を丸ごと人手で採点する構成。機械は 1 件も点を付けない。"""
    task = _task_version(
        _criterion(CORRECTNESS, "correctness", 0.6, HUMAN_SCORED),
        _criterion(DIAGRAM, "diagram", 0.4, HUMAN_SCORED),
    )
    run, _ = _run(task)

    assert run.criterion_scores == ()
    assert set(run.awaiting_human) == {CORRECTNESS, DIAGRAM}
    assert run.score_ratio == pytest.approx(0.0)
    assert run.routing is Routing.REVIEW_REQUIRED


def test_a_task_with_nothing_to_score_and_no_human_criterion_still_fails() -> None:
    """**落とすべき場合は落とす。** 採点されるはずの観点に誰も答えなかった。

    人採点の宣言が無いのに 1 件も点が付かないのは構成の誤りで、静かに
    0% の採点を作ると、その誤りは学習者の成績として現れる。
    """
    task = _task_version(_criterion(CORRECTNESS, "correctness", 1.0, None))
    pipeline, _ = _pipeline(deterministic=False)
    with pytest.raises(RuntimeError, match="no evaluator produced a score"):
        pipeline.run(task, _submission(), lambda _artifact: b"")
