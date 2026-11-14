"""`required` かつ `expected_count` の項目を「ちょうど満たした」場合（#703）。

既存の試験は、必須の項目で枚数が**足りない**側（3 枚要るところへ 2 枚）と、
必須でない項目でちょうど満たした側しか見ていなかった。`_is_met` の
`len(hits) >= expected_count` を `>` にしても全件が通った（#703 の変異）。

境界が 1 つずれると、必須の認定証を指定の枚数ちょうど出した学習者が「必須の
項目を満たしていません」で最低段階になる（ex01-3 の形）。決定的評価で確定する
ので AI 評価器にも回らず、誰も気づかない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_eval_text_pattern_check import EVALUATOR_ID, TextPatternCheck

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import ArtifactId, CriterionId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
REQUIRED_TEXT = "必須の項目"

CRITERION = RubricCriterion(
    id=CriterionId("crt_" + "1" * 32),
    code="certificates",
    title="認定証",
    description="d",
    weight=1.0,
    levels=(
        RubricLevel(level=0, label="未達", descriptor="無い", score_ratio=0.0),
        RubricLevel(level=1, label="達成", descriptor="揃う", score_ratio=1.0),
    ),
    evaluator_id=EVALUATOR_ID,
)


def _required_case(expected_count: int) -> TestCase:
    return TestCase(
        name="認定証",
        evaluator_id=EVALUATOR_ID,
        payload={
            "criterion": "certificates",
            "pattern": r"認定証",
            "expected_count": expected_count,
            "required": True,
        },
        hidden=False,
        weight=1.0,
    )


def _request(case: TestCase, bodies: tuple[str, ...]) -> EvaluationRequest:
    """`bodies` の本数だけ画像を提出したことにする（抽出器が起こした本文）。"""
    artifacts = []
    contents = {}
    for index, body in enumerate(bodies):
        artifact_id = ArtifactId(f"art_{index}" + "3" * 30)
        payload = body.encode("utf-8")
        artifacts.append(
            Artifact(
                id=artifact_id,
                submission_id=SUBMISSION,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.IMAGE,
                storage_key="k",
                content_hash=f"sha256:{index}",
                byte_size=len(payload),
                filename=f"認定証{index}.png",
                created_at=NOW,
            )
        )
        contents[artifact_id] = payload
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_network_python",
        statement="認定証を提出してください。",
        criteria=(CRITERION,),
        test_cases=(case,),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
        learner_id=UserId("usr_" + "7" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=tuple(artifacts),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        artifact_contents=contents,
        test_cases=(case,),
        learner_reference="y240040",
    )


def test_a_required_count_met_exactly_scores_top() -> None:
    outcome = TextPatternCheck().evaluate(
        _request(_required_case(expected_count=2), ("認定証A", "認定証B"))
    )
    score = outcome.scores[0]
    assert score.level == 1
    assert REQUIRED_TEXT not in score.rationale
    assert outcome.raw_output["counts"]["certificates"]["認定証"] == 2


def test_one_short_of_a_required_count_is_still_refused() -> None:
    """上の試験と対にする。境界の下側は最低段階のまま。"""
    outcome = TextPatternCheck().evaluate(
        _request(_required_case(expected_count=2), ("認定証A", "別の画像"))
    )
    score = outcome.scores[0]
    assert score.level == 0
    assert REQUIRED_TEXT in score.rationale
