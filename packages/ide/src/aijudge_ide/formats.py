"""エディタで書ける提出形式（2026-09-24 決定）。

**エディタのタブは aiJudge の課題 1 つに対応し、選べる形式はその課題の
提出形式（`Task.accepted_suffixes`、空ならコースの既定）に限る。** 課題が
受け付けない形式で書かせると、提出の段で断られる。

エディタで扱うのは 3 つだけである。

| 拡張子 | 扱い | 実行 |
|---|---|---|
| `.c` | プログラム | 採点の言語が C なら試しに実行できる |
| `.py` | プログラム | 採点の言語が Python なら試しに実行できる |
| `.md` | テキスト | しない。書いて提出するだけ（オンラインのレポート試験） |

**実行できるのは、選んだ形式が採点の言語と一致するときだけ**（設計書 §4.2）。
採点が C で動く課題で Python を走らせても、採点で何が起きるかの手がかりに
ならない。どの形式を選べるかは課題が決め、実行できるかは採点設定が決める ──
この 2 つは別の問いである。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_core import STREAMED_SUFFIXES, ArtifactKind, kind_for


@dataclass(frozen=True)
class EditorFormat:
    """エディタで書ける 1 形式。"""

    suffix: str
    # 学習者に見せる名前。
    label: str
    # Monaco の言語 ID。**C は `cpp` の定義で色分けする**（Monaco の C と C++ は
    # 共用の定義・設計書 §5.3）。
    monaco_language: str
    # 提出するときのファイル名。**プログラムは言語の表（`aijudge_toolchain`）の
    # `source_name` と同じにする** ── ファイルで出したときと同じ `Submission` に
    # なり、採点は区別できない（不変条件 I2）。
    filename: str
    # 試しに実行するときの言語名（`aijudge_toolchain.LANGUAGES` の鍵）。
    # テキストは None（実行しない）。
    run_language: str | None

    @property
    def kind(self) -> ArtifactKind:
        found = kind_for(self.suffix)
        if found is None:  # pragma: no cover - 下の表は uploads の表にある拡張子だけ
            raise ValueError(self.suffix)
        return found


EDITOR_FORMATS: dict[str, EditorFormat] = {
    ".c": EditorFormat(".c", "C", "cpp", "main.c", "c"),
    ".py": EditorFormat(".py", "Python", "python", "main.py", "python"),
    ".md": EditorFormat(".md", "テキスト（Markdown）", "markdown", "answer.md", None),
}


def editor_formats(accepted: tuple[str, ...]) -> tuple[EditorFormat, ...]:
    """課題が受け付ける形式のうち、エディタで書けるもの。**課題の並び順のまま。**"""
    return tuple(
        EDITOR_FORMATS[suffix]
        for suffix in dict.fromkeys(s.lower() for s in accepted)
        if suffix in EDITOR_FORMATS
    )


def attachable_suffixes(accepted: tuple[str, ...]) -> tuple[str, ...]:
    """エディタの画面から**ファイルを選んで**出せる形式（2026-09-25 決定）。

    課題が受け付ける形式のうち、エディタで書くもの（上の表）と動画を除いたもの ──
    画像・PDF など。**動画は出せない**: 分割送信・再開・保存期間を持つ専用の経路が
    あり（`STREAMED_SUFFIXES`）、エディタの画面はそれを持たない。動画は課題の画面の
    ファイル選択から出す（だから動画を受ける課題ではファイル選択を止められない）。
    """
    return tuple(
        suffix
        for suffix in dict.fromkeys(s.lower() for s in accepted)
        if suffix not in EDITOR_FORMATS
        and suffix not in STREAMED_SUFFIXES
        and kind_for(suffix) is not None
    )


def video_suffixes(accepted: tuple[str, ...]) -> tuple[str, ...]:
    """課題が受け付ける動画の形式。エディタの画面からは出せない（`attachable_suffixes`）。"""
    return tuple(s for s in dict.fromkeys(x.lower() for x in accepted) if s in STREAMED_SUFFIXES)
