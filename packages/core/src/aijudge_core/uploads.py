"""提出できるファイル形式。

**どの層も同じ表を見る。** 学習者の画面が受け付ける形式と、教員が指定した
形式と、保存時に付く `ArtifactKind` がずれると、「画面からは出せるのに
採点が種別を知らない」提出が生まれる。表はここにしか置かない。

指定は 2 段。**課題の指定がコースの既定を上書きする**（`allowed_suffixes`）。
コースで一度決めておけば個々の課題では触らずに済み、レポート 1 問だけ PDF を
許す、といった例外は課題側で足せる。

画像と PDF を受け付けるのは、手書きの答案とレポートのためである。どちらも
本文が直接読めないので、採点の前に本文へ変換する段が要る
（`ArtifactKind.is_document` と設計方針 §4 の Normalize 段）。**受け付ける
ことと採点できることは別**で、ここは前者だけを決める。
"""

from __future__ import annotations

from collections.abc import Iterable

from .submission import ArtifactKind

# 拡張子 → 種別。増やすときは採点側（評価器と正規化器）が扱えることを
# 確かめてから。ここに足すだけでは採点は追いつかない。
SUFFIX_KINDS: dict[str, ArtifactKind] = {
    ".c": ArtifactKind.CODE,
    ".py": ArtifactKind.CODE,
    ".java": ArtifactKind.CODE,
    ".tex": ArtifactKind.LATEX,
    ".md": ArtifactKind.MARKDOWN,
    ".pdf": ArtifactKind.PDF,
    ".jpg": ArtifactKind.IMAGE,
    ".jpeg": ArtifactKind.IMAGE,
    ".png": ArtifactKind.IMAGE,
    ".gif": ArtifactKind.IMAGE,
    # 動画。**通常の提出ルートでは受けない**（`ArtifactKind.is_streamed`）。
    # 数 GB になりうるので専用のストリーミング経路を通す。
    ".mp4": ArtifactKind.VIDEO,
    ".webm": ArtifactKind.VIDEO,
    ".mov": ArtifactKind.VIDEO,
}

# メモリに全体を載せてはいけない拡張子。専用ルート（`submit-video`）だけが受ける。
STREAMED_SUFFIXES: frozenset[str] = frozenset(
    suffix for suffix, kind in SUFFIX_KINDS.items() if kind.is_streamed
)

# 何も指定していないコースで受け付ける形式。**コードとテキストだけ。**
# 画像と PDF は本文が直接読めず、採点の前に変換の段が要るので、
# 教員が明示的に許した課題でだけ受け付ける。
DEFAULT_UPLOAD_SUFFIXES: tuple[str, ...] = (".c", ".py", ".java", ".tex", ".md")

ALL_UPLOAD_SUFFIXES: tuple[str, ...] = tuple(sorted(SUFFIX_KINDS))

# 拡張子 → MIME 型。**提出物を返すときに要る**（#75）。ここに無ければ
# `application/octet-stream` で返す ── 推測して間違えるより、ブラウザに
# 判断させないほうが安全である。
SUFFIX_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def content_type_for(filename: str | None) -> str:
    """このファイルを返すときの `Content-Type`。

    **型が分からないものを型があるように返さない。** 学習者が出したファイルを
    ブラウザに解釈させる経路なので、推測で `text/html` を返すような余地を
    作らない（`application/octet-stream` はブラウザが実行しない）。
    """
    lowered = (filename or "").lower()
    for suffix, mime in SUFFIX_CONTENT_TYPES.items():
        if lowered.endswith(suffix):
            return mime
    return "application/octet-stream"


# 画面で並べるときの区切り。**性質が違うものを混ぜない** ── コードとテキストは
# 本文がそのまま読めて採点器に渡せるが、PDF と画像は読めず、採点の前に本文へ
# 変換する段が要る（`ArtifactKind.is_document`）。並べて出すと、教員は
# 「1 つ増やすだけ」のつもりで採点の前提が変わる形式を選ぶ。
# 画面に出す選択肢。1 つの選択肢が複数の拡張子を持つことがある ──
# **`.jpg` と `.jpeg` は同じ形式の綴りの揺れ**で、別々に選ばせると
# 「`.jpg` は出せるのに `.jpeg` は弾かれる」が起きる。
SUFFIX_GROUPS: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    (
        "コード・テキスト",
        (
            (".c", (".c",)),
            (".py", (".py",)),
            (".java", (".java",)),
            (".tex", (".tex",)),
            (".md", (".md",)),
        ),
    ),
    (
        "PDF・画像",
        (
            (".pdf", (".pdf",)),
            (".jpg / .jpeg", (".jpg", ".jpeg")),
            (".png", (".png",)),
            (".gif", (".gif",)),
        ),
    ),
    (
        # 動画は取り込み経路が別（`ArtifactKind.is_streamed`）。教員が明示的に
        # 許した課題でだけ受け付ける ── 既定には入れない。
        "動画",
        (
            (".mp4", (".mp4",)),
            (".webm", (".webm",)),
            (".mov", (".mov",)),
        ),
    ),
)


def normalize_suffixes(raw: object) -> tuple[str, ...]:
    """入力された拡張子の並びを整える。

    先頭の `.` を補い、小文字にし、知らない拡張子は落とす。落とすのは、
    受け付けられない形式を「指定できたつもり」にさせないため ── 保存できて
    しまうと、学習者の画面にだけ出て提出時に弾かれる形式ができる。

    **カンマ区切りを 1 つの項目として受ける。** 画面の選択肢は綴りの揺れを
    まとめている（`.jpg / .jpeg`）ので、1 つのチェックから複数の拡張子が
    送られてくる。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        given = [raw]
    elif isinstance(raw, Iterable):
        given = [str(item) for item in raw]
    else:
        # 並びでも文字列でもないものは 1 項目として扱う（黙って捨てない）。
        given = [str(raw)]
    items = [part for entry in given for part in entry.split(",")]
    seen: list[str] = []
    for item in items:
        suffix = item.strip().lower()
        if not suffix:
            continue
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix in SUFFIX_KINDS and suffix not in seen:
            seen.append(suffix)
    return tuple(sorted(seen, key=ALL_UPLOAD_SUFFIXES.index))


def allowed_suffixes(
    task_suffixes: tuple[str, ...], course_suffixes: tuple[str, ...]
) -> tuple[str, ...]:
    """実際に受け付ける拡張子。**課題の指定がコースの既定を上書きする。**

    どちらも空なら組み込みの既定（コードとテキスト）。空の指定を「何も
    受け付けない」と読まないのは、そう読むと設定漏れが提出不能として
    現れ、原因が学習者側に見えるため。
    """
    return task_suffixes or course_suffixes or DEFAULT_UPLOAD_SUFFIXES


def kind_for(suffix: str) -> ArtifactKind | None:
    return SUFFIX_KINDS.get(suffix.lower())


__all__ = [
    "ALL_UPLOAD_SUFFIXES",
    "DEFAULT_UPLOAD_SUFFIXES",
    "STREAMED_SUFFIXES",
    "SUFFIX_GROUPS",
    "SUFFIX_KINDS",
    "allowed_suffixes",
    "content_type_for",
    "kind_for",
    "normalize_suffixes",
]
