"""AI 評価器が「何を読むか」の規則を固定する。

**提出時の種類で選ばない。** 正規化器が既に本文へ直しているので（設計方針
§4 step 1）、`Artifact.kind` は「学習者が何を出したか」の記録であって
「いま何が渡っているか」ではない。

種類で絞っていたときに何が起きたか: PDF で出されたレポート 19 件のうち
18 件で、本文が渡っているのに AI 観点が未採点のまま教員に積まれた。学習者
には「採点できなかった観点があります」と出て、点は保留になった。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
ARTIFACT = ArtifactId("art_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
SUBMISSION = SubmissionId("sub_" + "3" * 32)


def _request(kind: ArtifactKind, payload: bytes) -> EvaluationRequest:
    criterion = RubricCriterion(
        id=CriterionId("crt_" + "4" * 32),
        code="discussion",
        title="考察",
        description="結果から言えることと言えないことを区別しているか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="無い", score_ratio=0.0),
            RubricLevel(level=3, label="達成", descriptor="ある", score_ratio=1.0),
        ),
    )
    artifact = Artifact(
        id=ARTIFACT,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(payload),
        filename="report",
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=TaskVersion(
            id=VERSION,
            task_id=TaskId("tsk_" + "5" * 32),
            version=1,
            subject_profile="report_ja",
            statement="## レポート ##",
            criteria=(criterion,),
            max_score=100.0,
            provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
            created_at=NOW,
        ),
        submission=Submission(
            id=SUBMISSION,
            task_version_id=VERSION,
            learner_id=UserId("usr_" + "7" * 32),
            state=SubmissionState.SUBMITTED,
            artifacts=(artifact,),
            submitted_at=NOW,
            created_at=NOW,
        ),
        artifact_contents={ARTIFACT: payload},
        criterion=criterion,
    )


@pytest.mark.parametrize(
    "kind",
    [ArtifactKind.CODE, ArtifactKind.MARKDOWN, ArtifactKind.LATEX, ArtifactKind.PDF,
     ArtifactKind.DOCX],
)
def test_normalized_text_is_judged_whatever_was_submitted(kind: ArtifactKind) -> None:
    """**PDF と DOCX も含む。** 正規化のあとは同じ本文である。"""
    judge = RubricAiJudge.__new__(RubricAiJudge)  # ゲートウェイを作らずに _source だけ見る
    request = _request(kind, "1. 目的\n本実験の目的は性能の評価である。".encode())

    artifact_id, text = judge._source(request)

    assert artifact_id == ARTIFACT, kind
    assert text is not None and "本実験の目的" in text


def test_binary_content_is_not_judged() -> None:
    """変換できなかった提出をモデルに渡さない。

    `errors="replace"` で通すと、バイナリが「文字化けした本文」として渡り、
    モデルはそれらしい判定を返す。**根拠のない点が付くのが最悪の形。**
    """
    judge = RubricAiJudge.__new__(RubricAiJudge)
    request = _request(ArtifactKind.PDF, b"%PDF-1.4\n\x00\x01\x02\xff\xfe binary")

    assert judge._source(request) == (None, None)
