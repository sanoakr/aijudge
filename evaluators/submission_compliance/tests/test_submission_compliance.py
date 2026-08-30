"""提出の遵守判定の規則を固定する。

固定したいのは 3 つ。

既定は満点     **任意提出の課題では、出したこと自体に基礎点がある。**
               教員の実採点では、提出された 47 件のどの品質観点にも 0 が
               付いていなかった。既定を満点以外にすると、この基礎点が
               どこにも現れなくなる。
不備は 1 段ずつ 教員が使った段階は 3 つだけ（比率 1.0 / 0.5 / 0.1）。
               連続的な質の尺度ではなく、事務的な減点である。
遅延は見ない   **評価は遅延と独立に行う**（ADR 0013）。遅れたことは観点の
               段階ではなく、評価の結果に対する減点として外から当てる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_eval_submission_compliance import SubmissionCompliance

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorStatus,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import EvaluationRequest

DUE = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUBMISSION = SubmissionId("sub_" + "4" * 32)

# 教員が実際に使った 3 段（比率 1.0 / 0.5 / 0.1）。
LADDER = (
    RubricLevel(
        level=0, label="重大な不備", descriptor="出ていないか、複数の不備", score_ratio=0.1
    ),
    RubricLevel(
        level=2, label="不備あり", descriptor="遅延・名前・形式のいずれか", score_ratio=0.5
    ),
    RubricLevel(
        level=4, label="遵守", descriptor="間に合っていて名前も規則どおり", score_ratio=1.0
    ),
)


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CRITERION,
        code="format",
        title="体裁（4 点）",
        description="提出したか、間に合ったか、名前は規則どおりか。",
        # この試験では観点がこれ 1 つなので重みは 1.0
        # （課題版は重みの合計が 1.0 であることを求める）。
        weight=1.0,
        evaluator_id="submission_compliance",
        levels=LADDER,
    )


def _request(
    *,
    submitted_at: datetime | None = DUE,
    filename: str = "Y999999.pdf",
    kind: ArtifactKind = ArtifactKind.PDF,
    role: ArtifactRole = ArtifactRole.ORIGINAL,
    **options,
) -> EvaluationRequest:
    made = (
        Artifact(
            id=ARTIFACT,
            submission_id=SUBMISSION,
            role=role,
            kind=kind,
            storage_key="k",
            content_hash="sha256:x",
            byte_size=10,
            filename=filename,
            created_at=DUE,
        ),
    )
    task_version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="report_ja",
        statement="## レポート課題 ##\n\n性能を評価しなさい。",
        criteria=(_criterion(),),
        max_score=23.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=DUE,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
        learner_id=UserId("usr_" + "7" * 32),
        attempt=1,
        state=SubmissionState.SUBMITTED,
        artifacts=made,
        submitted_at=submitted_at,
        created_at=DUE,
    )
    return EvaluationRequest(task_version=task_version, submission=submission, options=options)


def _level(outcome) -> int:
    assert outcome.status is EvaluatorStatus.OK
    assert len(outcome.scores) == 1
    return outcome.scores[0].level


def test_handing_it_in_is_worth_full_marks() -> None:
    """**任意提出の課題では、出したこと自体が基礎点である。**"""
    outcome = SubmissionCompliance().evaluate(_request())
    assert _level(outcome) == 4
    assert "基礎点" in outcome.scores[0].rationale


def test_the_body_is_never_read() -> None:
    """本文が読めなくても満点。**体裁は文章の質ではない。**

    スキャン PDF や暗号化で本文が取り出せない提出はある。それは提出の
    遵守とは別のことなので、ここでは減点しない（受理の判定でやる）。
    """
    outcome = SubmissionCompliance().evaluate(_request())
    assert _level(outcome) == 4
    assert outcome.scores[0].evidence[0].note.endswith("本文は読んでいない）")


def test_lateness_does_not_change_the_level() -> None:
    """**遅延は観点の段階に効かない**（ADR 0013）。

    3 日遅れても、提出そのものが規則どおりなら満点の段。遅れたことは
    採点ワーカーが評価のあとに減点として当てる。混ぜると 2 つ壊れる ──
    この観点の κ が「提出の遵守」と「事務上の遅れ」の混合になり、教員が
    減点を免除したときに観点の段階まで動かす羽目になる。
    """
    outcome = SubmissionCompliance().evaluate(_request(submitted_at=DUE + timedelta(days=3)))
    assert _level(outcome) == 4
    assert "遅" not in outcome.scores[0].rationale
    assert "締切" not in outcome.scores[0].rationale


def test_a_wrong_filename_costs_one_rung() -> None:
    outcome = SubmissionCompliance().evaluate(
        _request(filename="report.pdf", filename_pattern=r"^[YT]\d{6}\.pdf$")
    )
    assert _level(outcome) == 2
    assert "ファイル名が規則に合いません" in outcome.scores[0].rationale


def test_two_faults_reach_the_bottom_rung() -> None:
    """不備は段ごとに下げる。3 段しかないので 2 つで最下段に着く。"""
    outcome = SubmissionCompliance().evaluate(
        _request(
            filename="report.docx",
            kind=ArtifactKind.DOCX,
            required_kinds=["pdf"],
            filename_pattern=r"^[YT]\d{6}\.pdf$",
        )
    )
    assert _level(outcome) == 0


def test_the_wrong_format_costs_one_rung() -> None:
    outcome = SubmissionCompliance().evaluate(
        _request(kind=ArtifactKind.DOCX, required_kinds=["pdf"])
    )
    assert _level(outcome) == 2


def test_nothing_gradable_gets_no_baseline() -> None:
    """**採点できる提出物が無ければ基礎点も無い。**

    提出そのものが空にはならない（SUBMITTED は成果物を 1 つ以上持つと
    ドメインが決めている）。起きるのは「図だけ添えて本体を出していない」
    ような形で、そのとき基礎点は付かない。
    """
    outcome = SubmissionCompliance().evaluate(_request(role=ArtifactRole.ATTACHMENT))
    assert _level(outcome) == 0
    assert "提出物がありません" in outcome.scores[0].rationale


def test_a_task_without_this_criterion_is_skipped() -> None:
    request = _request()
    stripped = request.model_copy(
        update={
            "task_version": request.task_version.model_copy(
                update={
                    "criteria": (
                        _criterion().model_copy(update={"evaluator_id": "report_structure"}),
                    )
                }
            )
        }
    )
    outcome = SubmissionCompliance().evaluate(stripped)
    assert outcome.status is EvaluatorStatus.SKIPPED
