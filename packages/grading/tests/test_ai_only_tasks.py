"""AI 観点しか持たない課題も決定的段階を通る（#80）。

**科目の宣言だけでは足りない。** `cs_intro_c` は `code_test_runner` を宣言して
いるが、取り込み器はテストケースの無い課題を AI 観点だけで構成する。以前は
「科目が決定的評価器を宣言しているのに点が 1 つも出ない」ことを異常として
扱っていたので、そういう課題の提出は再試行の上限まで落ち、**永久に採点
されなかった**（検証サーバで実際に起きた）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    HUMAN_SCORED,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    GradingPhase,
    Provenance,
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
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import (
    EvaluationOutcome,
    EvaluatorRegistry,
    GradingPipeline,
    SubjectProfile,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _criterion(code: str, evaluator: str | None, weight: float = 1.0) -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId(new_id("crt")),
        code=code,
        title=code,
        description=f"{code} を見る観点。",
        weight=weight,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="十分", score_ratio=1.0),
        ),
        evaluator_id=evaluator,
    )


def _task_version(*criteria: RubricCriterion) -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId(new_id("tsv")),
        task_id=TaskId(new_id("tsk")),
        version=1,
        subject_profile="cs_intro_c",
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


class _Silent:
    """何にも点を付けない決定的評価器。

    登録簿には**居る**（設定は正しい）が、この課題版には担当する観点が無い、
    という状況を作るために要る。
    """

    evaluator_id = "code_test_runner"
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request) -> EvaluationOutcome:
        return EvaluationOutcome(scores=())


def _pipeline() -> GradingPipeline:
    """決定的評価器を**宣言していて、登録簿にも居る**科目。ここが要点。"""
    registry = EvaluatorRegistry()
    registry.register(_Silent())
    return GradingPipeline(
        registry,
        SubjectProfile(name="cs_intro_c", deterministic=("code_test_runner",)),
    )


def test_a_task_whose_criteria_are_all_ai_passes_the_deterministic_phase() -> None:
    """**この形の課題は実在する。** 自動テストがまだ無い課題がそれ。"""
    task = _task_version(_criterion("correctness", None, 0.7), _criterion("readability", None, 0.3))

    run = _pipeline().run(
        task,
        _submission(),
        lambda _artifact: b"int main(){}",
        phase=GradingPhase.DETERMINISTIC,
    )

    # 点は出ないが、それは異常ではない ── AI 段階が続く。
    assert not run.criterion_scores
    assert {str(c) for c in run.unscored_criteria} == {str(c.id) for c in task.criteria}


def test_a_task_with_a_deterministic_criterion_still_fails_loudly() -> None:
    """**黙らせたいのは正常な場合だけ。** 誰も答えなかったのは依然として異常。

    評価器は登録簿に居て呼ばれてもいる。それでも点が返らないのは評価器側の
    不具合で、静かに未採点にすると気づけない。
    """
    import pytest

    task = _task_version(_criterion("correctness", "code_test_runner"))

    with pytest.raises(RuntimeError, match="no evaluator produced a score"):
        _pipeline().run(
            task,
            _submission(),
            lambda _artifact: b"int main(){}",
            phase=GradingPhase.DETERMINISTIC,
        )


def test_a_task_scored_entirely_by_hand_still_passes() -> None:
    """Issue #7 の規則を壊していないこと。"""
    task = _task_version(_criterion("diagram", HUMAN_SCORED))

    run = _pipeline().run(
        task,
        _submission(),
        lambda _artifact: b"int main(){}",
        phase=GradingPhase.DETERMINISTIC,
    )
    assert not run.criterion_scores
