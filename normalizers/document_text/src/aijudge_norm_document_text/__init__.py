"""提出された PDF / DOCX を、採点が読む本文に直す。

**採点の前に 1 回だけ変換する**（設計方針 §4 の step 1）。しなければ、
構造チェッカーと AI 評価器がそれぞれ PDF を開くことになり、2 つの実装が
食い違えば「構造は満たすのに AI には空に見える」が起きる。

対象は**文字が埋め込まれた文書だけ**。スキャン画像の PDF は扱わない ──
それは OCR で、学習者が提出前に書き起こしを確認する別の流れになる
（§4.2 / Phase 6）。ここで黙って OCR に流すと、読み取り誤りが採点結果
として学習者に届く。

依存は `pypdf`（純 Python）だけにしてある。pdftotext や mutool を呼ぶ形に
すると、採点機に別のパッケージ管理系の依存が増える。
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile

from aijudge_core import Artifact, ArtifactKind

logger = logging.getLogger(__name__)

# DOCX の本文の名前空間。
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 段落のあいだの空行を詰める。PDF の抽出は行が細かく割れるので、
# そのままだと「字数」も「節の並び」も読み取りにくい。
_BLANK_RUN = re.compile(r"\n{3,}")

# 抽出できたと見なす最小の文字数。これを下回るものは、文字が埋め込まれて
# いない（スキャン画像の）PDF と見て、変換しなかったことにする。
# **空文字を返さない。** 空を返すと下流は「本文が無いレポート」と読み、
# 学習者には 0 点の理由が「白紙」として出る。実際は読めなかっただけである。
MIN_TEXT_LENGTH = 40


class DocumentTextError(Exception):
    """変換できなかった。呼び出し側は元の内容をそのまま流す。"""


class DocumentText:
    normalizer_id = "document_text"

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind.is_document

    def normalize(self, artifact: Artifact, payload: bytes) -> bytes:
        """本文を UTF-8 で返す。読めなければ元の内容を返す。"""
        try:
            if artifact.kind is ArtifactKind.PDF:
                text = _from_pdf(payload)
            elif artifact.kind is ArtifactKind.DOCX:
                text = _from_docx(payload)
            else:  # pragma: no cover - applies_to で弾いている
                return payload
        except DocumentTextError as exc:
            logger.warning("could not read %s (%s): %s", artifact.id, artifact.kind.value, exc)
            return payload

        cleaned = _BLANK_RUN.sub("\n\n", text).strip()
        if len(cleaned) < MIN_TEXT_LENGTH:
            # 文字が埋め込まれていない。**変換しなかったことにする**
            # （空を「白紙のレポート」と読ませない）。
            logger.warning(
                "%s yielded only %d characters; treating it as not extractable",
                artifact.id,
                len(cleaned),
            )
            return payload
        return cleaned.encode("utf-8")


def _from_pdf(payload: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依存が入っていない構成
        raise DocumentTextError(f"pypdf is not available: {exc}") from exc

    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise DocumentTextError(f"not a readable PDF: {exc}") from exc
    if reader.is_encrypted:
        # 復号を試みない。パスワード付きの提出は受け付けた側の問題で、
        # 勝手に開けるべきものでもない。
        raise DocumentTextError("the PDF is encrypted")

    # **2 通りで抽出して、行が壊れていない方を採る。**
    #
    # 既定の抽出は PDF の内部順に文字を並べるので、生成器によっては
    # **1 文字ずつ改行された**テキストになる。実データで 1 件そうなり
    # （19 件中 1 件、88% の行が 1〜2 文字）、本文はあるのに節も字数も
    # 判定できなくなっていた。`layout` モードは座標を見て行を組み直すので
    # そういう PDF に強いが、常に良いわけではない（段組みで列が混ざる）。
    #
    # どちらが良いかは PDF ごとに違うので、両方やって選ぶ。判定には
    # 「1〜2 文字しかない行の割合」を使う ── 壊れ方がそこに出る。
    candidates: list[str] = []
    for mode in ("plain", "layout"):
        try:
            candidates.append(_extract(reader, mode))
        except Exception:
            logger.warning("PDF extraction in %s mode failed", mode, exc_info=True)
    if not candidates:
        raise DocumentTextError("no extraction mode produced text")
    return min(candidates, key=_brokenness)


def _extract(reader, mode: str) -> str:
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text(extraction_mode=mode) or "")
        except Exception:
            # 1 ページ壊れていても残りは読む。実データの提出には
            # 変換ツールが吐いた壊れたページが混ざる。
            logger.warning("page %d of a submitted PDF could not be read", index + 1)
    return "\n\n".join(pages)


def _brokenness(text: str) -> float:
    """行が壊れている度合い。1〜2 文字しかない行の割合。

    小さい方が良い。**字数の多い方を選ばない** ── 1 文字ずつ改行された
    テキストは改行の分だけ長くなるので、長さで選ぶと壊れた方を採る。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 1.0
    tiny = sum(1 for line in lines if len(line.strip()) <= 2)
    return tiny / len(lines)


def _from_docx(payload: bytes) -> str:
    """DOCX の本文を取り出す。

    `python-docx` を使わず標準ライブラリだけで読む。DOCX は zip の中の
    XML であり、必要なのは段落と表のテキストだけなので、依存を増やす
    価値がない。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise DocumentTextError(f"not a readable DOCX: {exc}") from exc
    try:
        document = archive.read("word/document.xml")
    except KeyError as exc:
        raise DocumentTextError("the DOCX has no word/document.xml") from exc

    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise DocumentTextError(f"malformed DOCX body: {exc}") from exc

    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        # 表のセルも同じ `w:p` で表されるので、段落を辿れば表も拾える。
        runs = [node.text or "" for node in paragraph.iter(f"{_W}t")]
        lines.append("".join(runs))
    return "\n".join(lines)


__all__ = ["MIN_TEXT_LENGTH", "DocumentText", "DocumentTextError"]
