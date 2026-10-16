"""1 つの科目に、画像の課題と PDF の課題が並ぶ（#352）。

固定したいのは 3 つ。

種類で選ぶ     画像は画像の抽出器、PDF は文書の抽出器が読む。宣言は科目に 1 つ。
先が勝つ       同じ種類を 2 つが名乗るなら、**先に書いたほうが動く**。
扱えない種類   どれも名乗らない種類は素通りする（原本のまま人へ回る）。

認定証（画像）とレポート（PDF）が同じ科目に並ぶのは実際の運用そのもので、
抽出器を 1 つしか当てられないと片方が黙って書き起こされない ── 採点は止まら
ないので、設定の誤りが結果に現れない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_upgrading_a_running_deployment import _StubJudge, _task_version

from aijudge_core import (
    HUMAN_SCORED,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Extraction,
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

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class _KindExtractor:
    """自分が名乗った種類だけを読み、どれを読んだか記録する替え玉。"""

    def __init__(self, extractor_id: str, kinds: tuple[ArtifactKind, ...]) -> None:
        self.extractor_id = extractor_id
        self._kinds = kinds
        self.seen: list[ArtifactKind] = []

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind in self._kinds

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        self.seen.append(artifact.kind)
        return Extraction(text=f"{self.extractor_id} が読んだ".encode(), engine=self.extractor_id)


def _submission(*kinds: ArtifactKind) -> Submission:
    submission_id = SubmissionId("sub_" + "6" * 32)
    suffixes = {ArtifactKind.IMAGE: "png", ArtifactKind.PDF: "pdf", ArtifactKind.CODE: "c"}
    artifacts = tuple(
        Artifact(
            id=ArtifactId(f"art_{index}" + "7" * 28),
            submission_id=submission_id,
            role=ArtifactRole.ORIGINAL,
            kind=kind,
            storage_key=f"k{index}",
            content_hash="sha256:x",
            byte_size=3,
            filename=f"f{index}.{suffixes[kind]}",
            created_at=NOW,
        )
        for index, kind in enumerate(kinds)
    )
    return Submission(
        id=submission_id,
        task_version_id=TaskVersionId("tsv_" + "8" * 32),
        learner_id=UserId("usr_" + "9" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=artifacts,
        submitted_at=NOW,
        created_at=NOW,
    )


def _pipeline(*extractors: _KindExtractor) -> GradingPipeline:
    registry = ExtractorRegistry()
    for extractor in extractors:
        registry.register(extractor)
    evaluators = EvaluatorRegistry()
    evaluators.register(_StubJudge())
    profile = SubjectProfile.model_validate(
        {
            "name": "mixed",
            "input": {"transcription": [e.extractor_id for e in extractors]},
            "ai_evaluators": ["stub_judge"],
        }
    )
    return GradingPipeline(evaluators, profile, registry)


def test_each_artifact_goes_to_the_extractor_that_claims_its_kind() -> None:
    images = _KindExtractor("images", (ArtifactKind.IMAGE,))
    documents = _KindExtractor("documents", (ArtifactKind.PDF, ArtifactKind.DOCX))
    pipeline = _pipeline(images, documents)

    run = pipeline.run(
        _task_version(),
        _submission(ArtifactKind.IMAGE, ArtifactKind.PDF),
        lambda artifact: b"bytes",
    )

    assert images.seen == [ArtifactKind.IMAGE]
    assert documents.seen == [ArtifactKind.PDF]
    # **両方の本文が記録に残る。** 片方しか読まないと、そちらの観点だけが
    # 採点され、もう片方は理由の分からない「読めない」になる。
    assert len(run.extractions) == 2
    assert {extraction.engine for extraction in run.extractions} == {"images", "documents"}


def test_the_first_one_declared_wins_when_both_claim_the_kind() -> None:
    """順序は宣言の順序である。**科目がどちらを使うか決められる。**"""
    first = _KindExtractor("first", (ArtifactKind.IMAGE,))
    second = _KindExtractor("second", (ArtifactKind.IMAGE,))
    pipeline = _pipeline(first, second)

    pipeline.run(
        _task_version(),
        _submission(ArtifactKind.IMAGE),
        lambda artifact: b"bytes",
    )

    assert first.seen == [ArtifactKind.IMAGE]
    assert second.seen == []


def test_a_kind_nobody_claims_passes_through_untouched() -> None:
    """扱えない種類は素通りする ── 原本のまま評価器へ渡り、人に回る。"""
    documents = _KindExtractor("documents", (ArtifactKind.PDF,))
    pipeline = _pipeline(documents)

    run = pipeline.run(
        _task_version(),
        _submission(ArtifactKind.IMAGE),
        lambda artifact: b"bytes",
    )

    assert documents.seen == []
    assert run.extractions == ()


# --------------------------------------------------------------------------
# 列の選択（#627）。ワーカーは成果物を読む前に、書き起こしが要るかを訊く。
# --------------------------------------------------------------------------


def test_an_image_needs_transcription_when_an_extractor_claims_it() -> None:
    """書き起こしの要る提出は、遅い列のワーカーへ回される。"""
    pipeline = _pipeline(_KindExtractor("images", (ArtifactKind.IMAGE,)))
    assert pipeline.needs_transcription(_task_version(), _submission(ArtifactKind.IMAGE))


def test_code_does_not_need_transcription() -> None:
    """**同じ科目のプログラムの提出は回さない。** 回すと、1 秒で終わる
    テスト実行が書き起こしの列の後ろに並ぶ ── 分けた意味が逆になる。"""
    pipeline = _pipeline(_KindExtractor("images", (ArtifactKind.IMAGE,)))
    assert not pipeline.needs_transcription(_task_version(), _submission(ArtifactKind.CODE))


def test_nothing_needs_transcription_when_the_subject_declares_no_extractor() -> None:
    pipeline = _pipeline()
    assert not pipeline.needs_transcription(_task_version(), _submission(ArtifactKind.IMAGE))


def test_nothing_needs_transcription_when_every_criterion_is_scored_by_a_human() -> None:
    """人だけが採点する課題では書き起こさない（`_extract` と同じ規則）。"""
    pipeline = _pipeline(_KindExtractor("images", (ArtifactKind.IMAGE,)))
    human_only = _task_version()
    human_only = human_only.model_copy(
        update={
            "criteria": tuple(
                criterion.model_copy(update={"evaluator_id": HUMAN_SCORED})
                for criterion in human_only.criteria
            )
        }
    )
    assert not pipeline.needs_transcription(human_only, _submission(ArtifactKind.IMAGE))
