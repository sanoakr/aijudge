"""画像の書き起こしが守る約束を固定する。

固定したいのは 4 つ。

書き起こすだけ    段階も判定も返さない。返すのは本文と出所だけ。
埋めない          読めなければ理由を添えて返す。**空を本文にしない。**
出所を持つ        どのモデルのどの版で起こしたか（P8）。
止めない          読めない画像 1 件で受付も採点も止めない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aijudge_ext_image_text import RETRY_REPEAT_PENALTY, ImageText

from aijudge_core import Artifact, ArtifactKind, ArtifactRole, Extractor
from aijudge_core.ids import ArtifactId, SubmissionId
from aijudge_llm_gateway import LlmGateway
from aijudge_llm_gateway.provider import ScriptedProvider

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def _artifact(kind: ArtifactKind = ArtifactKind.IMAGE) -> Artifact:
    return Artifact(
        id=ArtifactId("art_" + "1" * 32),
        submission_id=SubmissionId("sub_" + "2" * 32),
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(IMAGE_BYTES),
        filename="認定証.png",
        created_at=NOW,
    )


def _reader(
    *replies: object, finish_reasons: list[str | None] | None = None
) -> tuple[ImageText, ScriptedProvider]:
    provider = ScriptedProvider(
        [
            reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
            for reply in replies
        ],
        vision=True,
        finish_reasons=finish_reasons,
    )
    return ImageText(LlmGateway(provider), model="vl"), provider


def test_the_lines_become_the_body() -> None:
    reader, _ = _reader({"readable": True, "lines": ["認定証", "Y240040naka", ""]})
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert out.succeeded
    # 空行は落ちる。**本文は読める形にしてから渡す。**
    assert out.text.decode("utf-8") == "認定証\nY240040naka"


def test_it_records_where_the_text_came_from() -> None:
    """模型を使う抽出は出所を持つ（P8）。決定的な抽出との違いはここ。"""
    reader, _ = _reader({"readable": True, "lines": ["認定証"]})
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert out.engine == "image_text"
    assert out.model_id == "vl"
    assert out.prompt_version == "image_transcribe_ja@1"


def test_the_image_is_attached_to_the_call() -> None:
    reader, provider = _reader({"readable": True, "lines": ["認定証"]})
    reader.extract(_artifact(), IMAGE_BYTES)

    assert provider.calls[0].messages[-1].images, "画像が送られていない"


def test_an_unreadable_image_says_why_instead_of_returning_nothing() -> None:
    """**空を本文にしない。** 空を返すと下流は「白紙の提出」と読む。"""
    reader, _ = _reader({"readable": False, "lines": []})
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert not out.succeeded
    assert out.failed_reason


def test_a_transcription_with_no_text_is_not_a_blank_submission() -> None:
    reader, _ = _reader({"readable": True, "lines": ["", "  "]})
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert not out.succeeded


def test_a_dead_model_does_not_raise() -> None:
    """1 件の読めない画像で採点を止めない（`document_text` と同じ規則）。"""
    reader, _ = _reader()  # 応答が尽きている
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert not out.succeeded
    assert out.failed_reason


def test_it_leaves_documents_to_the_other_extractor() -> None:
    """**PDF は扱わない。** 描画に AGPL の依存が要る（ADR 0021）。"""
    reader, _ = _reader()
    assert reader.applies_to(ArtifactKind.IMAGE)
    assert not reader.applies_to(ArtifactKind.PDF)
    assert not reader.applies_to(ArtifactKind.CODE)


def test_it_satisfies_the_shared_extractor_contract() -> None:
    """**文書の抽出器と同じ契約に載る。** 対象が違うだけで仕事は同じ。"""
    reader, _ = _reader()
    assert isinstance(reader, Extractor)


def test_it_never_returns_a_score() -> None:
    """書き起こすだけで採点しない ── 返す型に段階が無いことで担保する。"""
    reader, _ = _reader({"readable": True, "lines": ["認定証"]})
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert not hasattr(out, "level")
    assert not hasattr(out, "score_ratio")


def test_a_truncated_transcription_is_retried_once_with_a_repeat_penalty() -> None:
    """縮退ループで切れた画像は、罰を掛けて 1 回だけやり直す（2026-10-01 の実測）。"""
    reader, provider = _reader(
        '{"readable": true, "lines": ["libbzip2-dev", "libbz',
        {"readable": True, "lines": ["cc hello.c"]},
        finish_reasons=["length", None],
    )
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert out.succeeded
    assert out.text.decode("utf-8") == "cc hello.c"
    first, second = provider.calls
    assert first.repeat_penalty is None, "最初の呼び出しは既定のまま"
    assert second.repeat_penalty == RETRY_REPEAT_PENALTY
    assert second.max_tokens == first.max_tokens, "予算は変えない（ADR 0021 §5）"


def test_a_transcription_that_is_truncated_twice_fails_without_a_third_call() -> None:
    reader, provider = _reader(
        '{"readable": true, "lines": ["a',
        '{"readable": true, "lines": ["b',
        finish_reasons=["length", "length"],
    )
    out = reader.extract(_artifact(), IMAGE_BYTES)

    assert not out.succeeded
    assert "OutputTruncated" in out.failed_reason
    assert len(provider.calls) == 2


def test_a_normal_transcription_is_never_penalised() -> None:
    """既定の呼び出しの動作を変えない ── 罰は必要になった呼び出しだけ。"""
    reader, provider = _reader({"readable": True, "lines": ["認定証"]})
    reader.extract(_artifact(), IMAGE_BYTES)

    assert [call.repeat_penalty for call in provider.calls] == [None]
