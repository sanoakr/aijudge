"""提出された文書を本文に直す規則を固定する。

固定したいのは 3 つ。

読める   文字が埋め込まれた PDF と DOCX から本文が取れる。
壊さない 1 文字ずつ改行される PDF がある。**行の壊れ方で抽出方法を選ぶ。**
         実データで 1 件そうなり、本文はあるのに節も字数も判定できなかった。
黙らない 読めなかったときに空文字を返さない。空は「白紙のレポート」と
         読まれ、学習者には 0 点の理由が白紙として出る。
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from aijudge_norm_document_text import MIN_TEXT_LENGTH, DocumentText, _brokenness

from aijudge_core import Artifact, ArtifactKind, ArtifactRole
from aijudge_core.ids import ArtifactId, SubmissionId

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


def _artifact(kind: ArtifactKind, name: str = "report.pdf") -> Artifact:
    return Artifact(
        id=ArtifactId("art_" + "0" * 32),
        submission_id=SubmissionId("sub_" + "0" * 32),
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=1,
        filename=name,
        created_at=NOW,
    )


def _docx(paragraphs: list[str]) -> bytes:
    """最小限の DOCX を組む（本文の段落だけ）。"""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_a_docx_body_becomes_text() -> None:
    paragraphs = ["1. 目的", "本実験の目的は性能を評価することである。" * 3]
    out = DocumentText().normalize(_artifact(ArtifactKind.DOCX, "r.docx"), _docx(paragraphs))

    text = out.decode("utf-8")
    assert "1. 目的" in text
    assert "性能を評価する" in text


def test_a_docx_that_is_not_a_zip_is_returned_untouched() -> None:
    """**変換できなくても採点は続ける。** 例外にすると 1 件で全員が止まる。"""
    payload = b"this is not a docx"
    assert DocumentText().normalize(_artifact(ArtifactKind.DOCX, "r.docx"), payload) == payload


def test_a_pdf_that_is_not_a_pdf_is_returned_untouched() -> None:
    payload = b"%not a pdf at all"
    assert DocumentText().normalize(_artifact(ArtifactKind.PDF), payload) == payload


def test_a_document_with_almost_no_text_is_treated_as_not_extractable() -> None:
    """スキャン画像の PDF を「白紙のレポート」と読ませない。

    空を返すと、学習者には 0 点の理由が白紙として出る。実際は読めなかった
    だけで、そこは人間が見る話である。
    """
    payload = _docx(["短い"])
    out = DocumentText().normalize(_artifact(ArtifactKind.DOCX, "r.docx"), payload)

    assert out == payload, "空に近い本文をそのまま通している"
    assert len("短い") < MIN_TEXT_LENGTH


def test_code_submissions_are_left_alone() -> None:
    normalizer = DocumentText()
    assert not normalizer.applies_to(ArtifactKind.CODE)
    assert normalizer.applies_to(ArtifactKind.PDF)
    assert normalizer.applies_to(ArtifactKind.DOCX)


def test_brokenness_prefers_whole_lines_over_single_characters() -> None:
    """**長さで選ばない。** 同じ本文でも、1 文字ずつ改行された方が長くなる。"""
    body = "人工的な遅延を用いたHTTPサーバの評価"
    broken = "\n".join(body)
    whole = body

    assert _brokenness(broken) > _brokenness(whole)
    assert _brokenness(whole) == 0.0
    # 長さで選ぶと壊れた方を採ってしまう、という関係をここで固定する。
    assert len(broken) > len(whole), "前提: 同じ本文でも壊れた方が字数は多い"
