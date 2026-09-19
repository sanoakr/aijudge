"""**運用中の配備に載せ替えても壊れないこと**を固定する。

抽出層の統合（ADR 0022）は、名前と置き場所を変える変更である。運用機
（`judge.math.ryukoku.ac.jp`）では既に提出と採点が進んでおり、そこには
配備が触らないものが 2 つある。

1. **科目プロファイル**は git のチェックアウトの外にある
   （`AIJUDGE_PROFILES_DIR`）。配備はこれを上書きしない ── 古い書き方の
   まま置かれている
2. **採点記録**は追記のみで書き換えない（P8）。配備の前に作られた
   `GradingRun` は、新しい欄を持たないまま残る

どちらも「新しい形に揃っているはず」と仮定した瞬間に、**配備した日に
採点が止まるか、静かに結果が変わる。**
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    Extraction,
    GradingRun,
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
from aijudge_grading import SubjectProfile

NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# 1. 運用機に置かれたままのプロファイル
# --------------------------------------------------------------------------


def test_a_profile_still_written_the_old_way_loads() -> None:
    """**配備した瞬間に全科目が読めなくなる**のを防ぐ。

    `extra="forbid"` のまま鍵を消すと、運用機のファイル（配備が触らない）が
    そのまま読み込み失敗になる。起動時に落ちるので採点が止まる。
    """
    profile = SubjectProfile.model_validate(
        {"name": "cs_network_python", "normalizers": [], "deterministic": ["code_test_runner"]}
    )
    assert profile.input.transcription is None
    assert profile.deterministic == ("code_test_runner",)


def test_the_old_key_becomes_the_new_one() -> None:
    """古い宣言は、新しい置き場所の宣言として読む。意味は同じである。"""
    profile = SubjectProfile.model_validate({"name": "report_ja", "normalizers": ["document_text"]})
    assert profile.input.transcription == "document_text"


def test_the_new_key_wins_when_both_are_written() -> None:
    """移行中は両方書かれうる。**新しい方を採る**（古い方は残骸である）。"""
    profile = SubjectProfile.model_validate(
        {
            "name": "x",
            "normalizers": ["document_text"],
            "input": {"transcription": "image_text"},
        }
    )
    assert profile.input.transcription == "image_text"


# --------------------------------------------------------------------------
# 2. 配備の前に作られた採点記録
# --------------------------------------------------------------------------


def _run_document() -> dict:
    """配備前に書かれた `GradingRun` の JSON（`extractions` を持たない）。"""
    return {
        "id": "grn_" + "1" * 32,
        "submission_id": "sub_" + "2" * 32,
        "context": {
            "task_version_id": "tsv_" + "3" * 32,
            "subject_profile": "report_ja",
            "rubric_version": "r@1",
            "input_hash": "sha256:x",
            "pipeline_version": "1",
        },
        "score_ratio": 0.0,
        "confidence": 1.0,
        "routing": "review_required",
        "awaiting_human": ["crt_" + "4" * 32],
        "created_at": "2026-09-10T00:00:00Z",
    }


def test_a_run_written_before_the_upgrade_still_loads() -> None:
    run = GradingRun.model_validate(_run_document())
    assert run.extractions == ()


def test_an_extraction_survives_the_json_round_trip() -> None:
    """記録は JSON で保存される（`_dump` は `mode="json"`）。

    本文は `bytes` なので、往復で壊れないことを固定する ── 壊れると、
    再採点でも画面でも「何を読んで採点したか」が出せなくなる（P8）。
    """
    extraction = Extraction(
        artifact_id="art_" + "5" * 32,
        text="認定証\nY240040 中村".encode(),
        engine="image_text",
        model_id="qwen3-vl:8b",
        prompt_version="image_transcribe_ja@1",
    )
    back = Extraction.model_validate(extraction.model_dump(mode="json"))
    assert back.text == extraction.text
    assert back.model_id == "qwen3-vl:8b"


# --------------------------------------------------------------------------
# 3. 配備をまたいだ AI フェーズ
# --------------------------------------------------------------------------


class _CountingExtractor:
    """何回呼ばれたかを数える抽出器。"""

    extractor_id = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind is ArtifactKind.PDF

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        self.calls += 1
        return Extraction(text=b"extracted body", engine=self.extractor_id)


def _submission() -> Submission:
    submission_id = SubmissionId("sub_" + "6" * 32)
    artifact = Artifact(
        id=ArtifactId("art_" + "7" * 32),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.PDF,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=3,
        filename="r.pdf",
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


def _task_version(evaluator_id: str = "stub_judge") -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "8" * 32),
        task_id=TaskId("tsk_" + "a" * 32),
        version=1,
        subject_profile="report_ja",
        statement="レポートを出しなさい。",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "b" * 32),
                code="body",
                title="本文",
                description="本文があるか。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="無", descriptor="無い", score_ratio=0.0),
                    RubricLevel(level=1, label="有", descriptor="ある", score_ratio=1.0),
                ),
                evaluator_id=evaluator_id,
            ),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "c" * 32)),
        created_at=NOW,
    )


class _StubJudge:
    """観点 1 つに点を付けるだけの替え玉（採点の中身は見ない）。

    **抽出の回数だけを見たいので、採点は成立させる。** 1 点も付かない run は
    パイプラインが弾く（設定ミスを見逃さないための検査）。
    """

    evaluator_id = "stub_judge"
    kind = EvaluatorKind.AI

    def evaluate(self, request):
        from aijudge_core import CriterionScore, Evidence, WholeSpan, new_id
        from aijudge_core.ids import CriterionScoreId, EvaluatorResultId
        from aijudge_grading.protocol import EvaluationOutcome

        criterion = request.criterion
        assert criterion is not None
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
                    conclusive=False,
                    # AI の判定は根拠を持たなければ保存できない（P4）。
                    evidence=(
                        Evidence(
                            artifact_id=ArtifactId("art_" + "7" * 32),
                            artifact_content_hash="sha256:x",
                            span=WholeSpan(),
                            note="替え玉",
                        ),
                    ),
                    rationale="替え玉",
                ),
            )
        )


def _pipeline(extractor: _CountingExtractor):
    from aijudge_grading import EvaluatorRegistry, ExtractorRegistry, GradingPipeline

    extractors = ExtractorRegistry()
    extractors.register(extractor)
    evaluators = EvaluatorRegistry()
    evaluators.register(_StubJudge())
    profile = SubjectProfile.model_validate(
        {
            "name": "report_ja",
            "input": {"transcription": "counting"},
            "ai_evaluators": ["stub_judge"],
        }
    )
    return GradingPipeline(evaluators, profile, extractors)


def test_the_text_is_extracted_once_and_reused_by_the_ai_phase() -> None:
    """**フェーズごとに取り直さない。**

    取り直すと、模型を使う抽出では 2 回の結果が一致せず、「決定的評価が
    見た本文」と「AI 評価器が見た本文」が違うものになる。
    """
    from aijudge_core import GradingPhase

    extractor = _CountingExtractor()
    pipeline = _pipeline(extractor)
    submission, version = _submission(), _task_version()

    base = pipeline.run(version, submission, lambda a: b"pdf", phase=GradingPhase.DETERMINISTIC)
    assert extractor.calls == 1
    assert base.extractions and base.extractions[0].text == b"extracted body"

    pipeline.run(version, submission, lambda a: b"pdf", phase=GradingPhase.AI, base=base)
    assert extractor.calls == 1, "AI フェーズが本文を取り直している"


def test_an_ai_phase_on_a_pre_upgrade_base_extracts_rather_than_reading_the_original() -> None:
    """**配備をまたいだ提出だけ静かに採点が変わる**のを防ぐ。

    配備前に作られた土台は `extractions` を持たない。空をそのまま信じると、
    その AI フェーズだけが本文ではなく原本（PDF のバイト列）を読む。
    """
    from aijudge_core import GradingPhase

    extractor = _CountingExtractor()
    pipeline = _pipeline(extractor)
    submission, version = _submission(), _task_version()

    base = pipeline.run(version, submission, lambda a: b"pdf", phase=GradingPhase.DETERMINISTIC)
    # 配備前の土台を模す ── 中身は同じで、抽出の記録だけが無い。
    older = base.model_copy(update={"extractions": ()})
    extractor.calls = 0

    pipeline.run(version, submission, lambda a: b"pdf", phase=GradingPhase.AI, base=older)
    assert extractor.calls == 1, "土台が空なのに取り直していない"


def test_a_task_scored_only_by_people_is_not_transcribed() -> None:
    """**誰も読まないなら取り出さない。**

    全観点を人が採点する課題では、機械は 1 点も付けない（人採点の観点に
    付いた判定は捨てられる・ADR 0015）ので、書き起こしても行き先が無い。
    それでも走らせると画像 1 枚あたり 20〜40 秒を捨てる ── 科目プロファイルは
    複数の課題で共有されるので、同じコースに「機械が読む画像の課題」と
    「教員が目で見る画像の課題」が並ぶことは実際にある。
    """
    from aijudge_core import GradingPhase

    extractor = _CountingExtractor()
    pipeline = _pipeline(extractor)
    run = pipeline.run(
        _task_version(evaluator_id="__human__"),
        _submission(),
        lambda a: b"pdf",
        phase=GradingPhase.DETERMINISTIC,
    )
    assert extractor.calls == 0, "誰も読まない本文を取り出している"
    assert run.extractions == ()
