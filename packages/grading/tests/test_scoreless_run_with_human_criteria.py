"""点が 1 つも付かない段階でも、人採点の観点が同居する課題は採点が通る（#677）。

観点が「AI が見る 0.6」と「教員が目で見る 0.4（`HUMAN_SCORED`）」の課題。ワーカーは必ず
決定的段階から走らせるので、決定的評価器の担当観点が無いこの段階では点が 1 つも付かない。
以前は人採点の分（0.4）だけが「0% として数える重み」に入り、`aggregate((), zero_weight=0.4)`
が重みの合計 1.0 に満たず `ValueError` になった ── ワーカーは再試行の上限まで同じ理由で
落ち続け、提出は採点待ちのまま残った（#80 と同じ現れ方）。

固定したいのは 3 つ。

通る          決定的段階が落ちず、AI 観点は未採点・人採点の観点は採点待ちの run になる
総合点は無い  総合点を 0 と出さない（確信度も 0）。点が定まるのは AI 段階と教員のあと
従来どおり    全観点が人採点の課題は、これまでどおり通る
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
        subject_profile="cs_lang_c_intro",
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
        SubjectProfile(name="cs_lang_c_intro", deterministic=("code_test_runner",)),
    )


def test_a_run_with_no_score_and_a_human_criterion_is_created_not_raised() -> None:
    task = _task_version(
        _criterion("explain", None, 0.6),
        _criterion("read_by_teacher", HUMAN_SCORED, 0.4),
    )

    run = _pipeline().run(
        task,
        _submission(),
        lambda _artifact: b"int main(){}",
        phase=GradingPhase.DETERMINISTIC,
    )

    assert not run.criterion_scores
    explain, teacher = task.criteria
    assert run.unscored_criteria == (explain.id,)
    assert run.awaiting_human == (teacher.id,)
    assert run.is_provisional


def test_such_a_run_has_no_total_to_show() -> None:
    task = _task_version(
        _criterion("explain", None, 0.6),
        _criterion("read_by_teacher", HUMAN_SCORED, 0.4),
    )

    run = _pipeline().run(
        task,
        _submission(),
        lambda _artifact: b"int main(){}",
        phase=GradingPhase.DETERMINISTIC,
    )

    # 「0 点」ではなく「無い」── 観点は全部 unscored / 採点待ちに入っている。
    assert run.score_ratio == 0.0 and run.confidence == 0.0
    assert run.needs_review


def test_a_task_scored_entirely_by_hand_still_passes() -> None:
    """全観点が人採点なら、重みは全部「0% として数える」側に入る（Issue #7）。"""
    task = _task_version(_criterion("diagram", HUMAN_SCORED))

    run = _pipeline().run(
        task,
        _submission(),
        lambda _artifact: b"int main(){}",
        phase=GradingPhase.DETERMINISTIC,
    )

    assert not run.criterion_scores
    assert run.awaiting_human == (task.criteria[0].id,)
    assert run.confidence == 1.0
