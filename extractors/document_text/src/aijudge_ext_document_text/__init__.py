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
import json
import logging
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

from aijudge_core import Artifact, ArtifactKind, Extraction

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
# 解析を子プロセスで動かすときの上限（#420）。pypdf は学習者の出した PDF を
# ワーカーのプロセス内で上限なしに読んでいた ── 細工した PDF 1 件で、
# ワーカーが長時間止まる・メモリを使い切る。実データの PDF は数秒で終わる。
PARSE_TIMEOUT_SECONDS = 60
PARSE_MEMORY_BYTES = 1024 * 1024 * 1024


class DocumentTextError(Exception):
    """変換できなかった。呼び出し側は元の内容をそのまま流す。"""


class DocumentText:
    """文書から本文を取り出す抽出器。**決定的で、模型を使わない。**

    `image_text`（画像 → 本文）と同じ契約に載る ── 対象が違うだけで仕事は
    同じである（`aijudge_core.extraction`）。こちらは出所を持たない
    （`model_id` も `prompt_version` も無い）。pypdf が同じ PDF に対して
    毎回同じ本文を返すので、記録すべき「どれで起こしたか」が engine 以外に
    無いためである。
    """

    extractor_id = "document_text"

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind.is_document

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        """本文を返す。読めなければ理由を添えて返す（例外にしない）。"""
        try:
            text = _parse(artifact.kind, payload)
        except DocumentTextError as exc:
            logger.warning("could not read %s (%s): %s", artifact.id, artifact.kind.value, exc)
            return self._failed(str(exc))

        cleaned = _BLANK_RUN.sub("\n\n", text).strip()
        if len(cleaned) < MIN_TEXT_LENGTH:
            # 文字が埋め込まれていない。**取り出せなかったことにする**
            # （空を「白紙のレポート」と読ませない）。
            logger.warning(
                "%s yielded only %d characters; treating it as not extractable",
                artifact.id,
                len(cleaned),
            )
            return self._failed(
                f"取り出せた文字が {len(cleaned)} 字しかありません"
                "（文字が埋め込まれていない PDF の可能性があります）"
            )
        return Extraction(text=cleaned.encode("utf-8"), engine=self.extractor_id)

    def _failed(self, reason: str) -> Extraction:
        return Extraction(engine=self.extractor_id, failed_reason=reason)


def text_of(payload: bytes, kind: ArtifactKind) -> str:
    """文書の本文を取り出す。読めなければ `DocumentTextError`。

    **採点の外からも使う。** シラバスの読み取り（`aijudge_admin.syllabus`）が
    同じ抽出を要る ── 別に実装すると、片方だけが壊れた PDF を読めるという
    差が出て、原因の切り分けができなくなる。
    """
    text = _parse(kind, payload)
    cleaned = _BLANK_RUN.sub("\n\n", text).strip()
    if len(cleaned) < MIN_TEXT_LENGTH:
        # 文字が埋め込まれていない（スキャン画像の PDF）。
        raise DocumentTextError(
            "文字が埋め込まれていません（スキャン画像の PDF の可能性があります）"
        )
    return cleaned


def _parse(kind: ArtifactKind, payload: bytes) -> str:
    """文書を解析する。**子プロセスで、時間とメモリの上限を付けて**（#420）。

    上限を超えたら「読めなかった」として扱う（`DocumentTextError`）。採点は
    ほかの経路と同じく、抽出できなかった提出として続く。
    """
    if kind not in (ArtifactKind.PDF, ArtifactKind.DOCX):
        raise DocumentTextError(f"{kind.value} は本文を取り出せる形式ではありません")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", f"{__name__}._child", kind.value, str(PARSE_MEMORY_BYTES)],
            input=payload,
            capture_output=True,
            timeout=PARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocumentTextError(f"解析が {PARSE_TIMEOUT_SECONDS} 秒で終わりませんでした") from exc
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # 上限で殺された（SIGKILL・SIGSEGV）など。出力が無い。
        raise DocumentTextError(
            f"解析が途中で止まりました（終了コード {completed.returncode}）"
        ) from None
    if "error" in result:
        raise DocumentTextError(result["error"])
    return str(result["text"])


def _parse_in_process(kind: ArtifactKind, payload: bytes) -> str:
    """子プロセスの中で呼ぶ本体。**親から直接呼ばない。**"""
    if kind is ArtifactKind.PDF:
        return _from_pdf(payload)
    return _from_docx(payload)


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


__all__ = ["MIN_TEXT_LENGTH", "DocumentText", "DocumentTextError", "text_of"]
