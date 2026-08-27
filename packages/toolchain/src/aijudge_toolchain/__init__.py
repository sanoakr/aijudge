"""提出プログラムを言語ごとにどう扱うか。

**依存を持たない。** 複数の評価器が同じ知識を要る（テスト実行の評価器と、
伴走プロセスを使うネットワーク課題の評価器）。片方に置いて他方が import
すると評価器どうしが結合し、片方を差し替えられなくなる。観測レコードを
独立させたのと同じ理由（ADR 0007）。

**採点エンジンには置かない。** エンジンが言語を知った瞬間、科目の追加が
エンジンの改修になる（ADR 0002）。`packages/core` と `packages/grading` に
言語固有の語が現れないことはテストで固定してある。
"""

from __future__ import annotations

from typing import NamedTuple

BINARY_NAME = "main"

OPTION_LANGUAGE = "language"
OPTION_IMAGE = "image"
DEFAULT_LANGUAGE = "c"


class UnknownLanguage(Exception):
    """科目プロファイルが知らない言語を指定した。

    採点を止める。既定に落とすと、言語違いが「全員 0 点」として現れ、
    原因が提出物の側にあるように見える。
    """


class Language(NamedTuple):
    """1 言語の扱い方。

    `compile_argv` が None ならコンパイル段階を飛ばす（スクリプト言語）。
    飛ばすのは速さのためではなく、**「コンパイルエラー」という結果が
    存在しない**言語で、構文エラーを実行時の失敗として扱うため。
    """

    source_name: str
    compile_argv: tuple[str, ...] | None
    run_argv: tuple[str, ...]
    # 学習者に見せる名前。エラーメッセージに出る。
    label: str


LANGUAGES: dict[str, Language] = {
    "c": Language(
        source_name="main.c",
        # -O0 なのは、最適化で消える未定義動作を採点で拾えるようにするため。
        compile_argv=("cc", "-std=c11", "-O0", "-o", BINARY_NAME, "main.c"),
        run_argv=(f"./{BINARY_NAME}",),
        label="C",
    ),
    "python": Language(
        source_name="main.py",
        compile_argv=None,
        # -I で分離モードにする。環境変数（PYTHONPATH 等）と利用者の
        # site-packages を無視するので、提出の挙動が採点機の状態に依存しない。
        run_argv=("python3", "-I", "main.py"),
        label="Python",
    ),
}


def resolve_language(options: dict[str, object]) -> Language:
    """科目プロファイルから言語を引く。

    知らない言語名は**例外にする**（`UnknownLanguage` の docstring 参照）。
    """
    name = str(options.get(OPTION_LANGUAGE, DEFAULT_LANGUAGE)).strip().lower()
    if name not in LANGUAGES:
        raise UnknownLanguage(
            f"language {name!r} is not supported; pick one of {sorted(LANGUAGES)}"
        )
    return LANGUAGES[name]
