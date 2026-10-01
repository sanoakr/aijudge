"""本文を取り出せなかった提出は、落とさず未採点にして人へ回す。

固定したいのは 3 つ。

落とさない      抽出が失敗して点が 1 つも出ないとき、例外にせず run を作る。
                例外にすると再試行の上限まで同じ理由で失敗し、提出は黙って
                採点待ちのままになる（2026-10-01、書き起こしの打ち切り）。
人へ回る        全観点が `unscored`、ルーティングは REVIEW_REQUIRED、総合点は無い。
設定の誤りは別  抽出が成功しているのに誰も点を付けなかったのは、今までどおり異常。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_upgrading_a_running_deployment import _task_version

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    Extraction,
    Routing,
    Submission,
    SubmissionState,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_grading import (
    EvaluatorRegistry,
    ExtractorRegistry,
    GradingPipeline,
    SubjectProfile,
)
from aijudge_grading.protocol import EvaluationOutcome, EvaluatorStatus

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class _Reader:
    extractor_id = "reader"

    def __init__(self, extraction: Extraction) -> None:
        self._extraction = extraction

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind is ArtifactKind.IMAGE

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        return self._extraction


class _SilentJudge:
    """本文が無いときの `rubric_ai_judge` と同じ、点を返さない AI 評価器。"""

    evaluator_id = "silent_judge"
    kind = EvaluatorKind.AI

    def evaluate(self, request):
        return EvaluationOutcome(
            status=EvaluatorStatus.SKIPPED,
            raw_output={"reason": "no textual artifact to judge"},
        )


def _submission() -> Submission:
    submission_id = SubmissionId("sub_" + "6" * 32)
    artifact = Artifact(
        id=ArtifactId("art_" + "7" * 32),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.IMAGE,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=3,
        filename="shot.png",
        created_at=NOW,
    )
    return Submission(
        id=submission_id,
        task_version_id=TaskVersionId("tsv_" + "8" * 32),
        learner_id=UserId("usr_" + "9" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        submitted_at=NOW,
        created_at=NOW,
    )


def _pipeline(extraction: Extraction) -> GradingPipeline:
    extractors = ExtractorRegistry()
    extractors.register(_Reader(extraction))
    evaluators = EvaluatorRegistry()
    evaluators.register(_SilentJudge())
    profile = SubjectProfile.model_validate(
        {
            "name": "images",
            "input": {"transcription": ["reader"]},
            "ai_evaluators": ["silent_judge"],
        }
    )
    return GradingPipeline(evaluators, profile, extractors)


def test_a_failed_extraction_leaves_the_submission_unscored_for_a_human() -> None:
    failed = Extraction(engine="reader", failed_reason="OutputTruncated: hit its output budget")
    task = _task_version()

    run = _pipeline(failed).run(task, _submission(), lambda _artifact: b"bytes")

    assert run.criterion_scores == ()
    assert {str(c) for c in run.unscored_criteria} == {str(c.id) for c in task.criteria}
    assert run.routing is Routing.REVIEW_REQUIRED
    # **理由が記録に残る**（P8）。人が「なぜ未採点か」を画面で読める。
    assert [e.failed_reason for e in run.extractions] == [failed.failed_reason]


def test_a_successful_extraction_with_no_score_is_still_an_error() -> None:
    """**黙らせたいのは入力が読めなかった場合だけ。** 読めたのに誰も答えなかった
    のは評価器か設定の不具合で、静かに未採点にすると気づけない。"""
    readable = Extraction(text=b"cc hello.c", engine="reader")

    with pytest.raises(RuntimeError, match="no evaluator produced a score"):
        _pipeline(readable).run(_task_version(), _submission(), lambda _artifact: b"bytes")
