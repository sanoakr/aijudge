"""採点が読んだ本文を、原本の隣に出す（#354）。

固定したいのは 5 つ。

原本の隣       画像・PDF は原本を表示し、その下に書き起こしを置く。
出所を添える   どの抽出器・どの模型が起こしたか（再現性の一部・P8）。
失敗も出す     読めなかったものは理由を出す。空欄と失敗を同じ顔にしない。
走っていない   全観点が人採点の課題では、書き起こしを**していない**と書く。
他人のものは出さない  別の提出の書き起こしが紛れ込まない。

**なぜ並べるか**: 評価器に渡ったのは書き起こした本文で、教員が見ているのは
原本である（`GradingPipeline._apply` が中身を差し替える）。名前を 1 文字
読み違えた、図のキャプションが落ちた、2 段組が交互に混ざった ── どれも
「採点がおかしい」ではなく「読んだものが違う」として現れるので、両方を
並べない限り見つけようがない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Extraction,
    GradingRun,
    Submission,
    SubmissionState,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_reviewconsole.app import TEMPLATES, Console

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "1" * 32)
IMAGE = ArtifactId("art_" + "2" * 32)
OTHER = ArtifactId("art_" + "3" * 32)


def _submission() -> Submission:
    return Submission(
        id=SUBMISSION,
        task_version_id=TaskVersionId("tsv_" + "4" * 32),
        learner_id=UserId("usr_" + "5" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=(
            Artifact(
                id=IMAGE,
                submission_id=SUBMISSION,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.IMAGE,
                storage_key="k",
                content_hash="sha256:x",
                byte_size=3,
                filename="cert.png",
                created_at=NOW,
            ),
        ),
        submitted_at=NOW,
        created_at=NOW,
    )


def _run(*extractions: Extraction) -> GradingRun:
    return GradingRun.model_validate(
        {
            "id": "grn_" + "6" * 32,
            "submission_id": str(SUBMISSION),
            "context": {
                "task_version_id": "tsv_" + "4" * 32,
                "subject_profile": "cs_network_python",
                "rubric_version": "r@1",
                "input_hash": "sha256:x",
                "pipeline_version": "1",
            },
            "extractions": [e.model_dump() for e in extractions],
            "score_ratio": 0.0,
            "confidence": 1.0,
            "routing": "review_required",
            # 点の無い run は理由を要る（人待ちか、採点できなかったか）。
            "awaiting_human": ["crt_" + "7" * 32],
            "created_at": "2026-09-21T00:00:00Z",
        }
    )


def test_the_transcript_is_keyed_by_the_artifact_it_came_from() -> None:
    run = _run(
        Extraction(
            artifact_id=str(IMAGE),
            text="認定証 Y240040".encode(),
            engine="image_text",
            model_id="qwen3-vl:8b",
        )
    )

    rows = Console.transcripts_of(None, _submission(), run)  # type: ignore[arg-type]

    assert rows[str(IMAGE)]["text"] == "認定証 Y240040"
    # 出所を残す（P8）。どの模型のどの版が起こした本文かは再現性の一部。
    assert rows[str(IMAGE)]["engine"] == "image_text"
    assert rows[str(IMAGE)]["model_id"] == "qwen3-vl:8b"


def test_a_transcript_of_another_submission_does_not_leak_in() -> None:
    """対応づけは記録の側の仕事なので、**照合を省かない**。"""
    run = _run(Extraction(artifact_id=str(OTHER), text="よそのもの".encode(), engine="image_text"))

    rows = Console.transcripts_of(None, _submission(), run)  # type: ignore[arg-type]

    assert rows == {}


def test_without_a_run_there_is_nothing_to_show() -> None:
    assert Console.transcripts_of(None, _submission(), None) == {}  # type: ignore[arg-type]


def _render(**context) -> str:
    return TEMPLATES.env.get_template("_transcript.html").render(
        file={"id": str(IMAGE), "is_image": True, "is_pdf": False}, **context
    )


def test_the_page_shows_the_text_and_where_it_came_from() -> None:
    html = _render(
        transcripts={
            str(IMAGE): {
                "text": "認定証 Y240040",
                "engine": "image_text",
                "model_id": "qwen3-vl:8b",
                "failed_reason": None,
            }
        }
    )

    assert "認定証 Y240040" in html
    assert "image_text" in html
    assert "qwen3-vl:8b" in html


def test_a_failed_extraction_says_why_and_that_it_is_not_a_zero() -> None:
    """**空欄と失敗を同じ顔にしない。** 読めないのは学習者の落ち度とは限らない。"""
    html = _render(
        transcripts={
            str(IMAGE): {
                "text": "",
                "engine": "image_text",
                "model_id": None,
                "failed_reason": "画像の中身がありません",
            }
        }
    )

    assert "画像の中身がありません" in html
    assert "0 点ではありません" in html


def test_a_human_scored_task_says_it_did_not_transcribe() -> None:
    """無いことの理由を出す ── 失敗したのか、初めから走っていないのか。"""
    html = _render(transcripts={}, transcription_skipped=True)

    assert "書き起こしは行っていません" in html


def test_nothing_is_said_when_a_transcript_is_merely_absent() -> None:
    """走る課題で書き起こしがまだ無いときに、誤った説明を出さない。"""
    html = _render(transcripts={}, transcription_skipped=False)

    assert html.strip() == ""
