"""課題文の描画。

課題文は Markdown である（取り込み器が `desc.md` を読む）。**その形式を
決めているのがこのパッケージなので、安全に描画する方法も同じ場所に置く。**
表示側のアプリ 2 つ（学習者向けと教員向け）が別々に設定を持つと、片方で
安全側の設定が抜ける。

実測（2026-08-28）で必要だと分かった。学生 UI に生の Markdown が出ており、
`## partialsum.py` や ```` ```Python ```` がそのまま表示されていた。
プログラミング課題の課題文はコードブロックが本体なので、これは読めない。

**生の HTML は通さない。** 課題文は当面は教員が書いたものだが、AI 作問
（Phase 4）が入ればモデルの出力になる。そのとき `<script>` が通る経路が
あってはならないので、最初から塞いでおく。
"""

from __future__ import annotations

from functools import lru_cache

# 数式は**サーバ側で MathML に変換する。**
#
# CDN の KaTeX を読むと、課題文が読めるかどうかが外向きの通信に依存する。
# この配置（tailscale の内側の 1 台）でそれを前提にすると、回線が細い日に
# 数式だけが崩れた課題文が出る ── しかも原因が学習者からは分からない。
# MathML なら追加のスクリプトも要らず、`html=False` の方針とも矛盾しない。
#
# **変換できない数式は元の文字列のまま出す。** 課題文が読めないより、
# `$\sum_{i=1}^{n}$` が見えている方がまし（提出はできる）。


@lru_cache(maxsize=1)
def _renderer():
    from markdown_it import MarkdownIt
    from mdit_py_plugins.dollarmath import dollarmath_plugin

    # `html=False` が要点。課題文に埋め込まれた HTML をそのまま出さない。
    # `linkify` は URL を自動リンクにする（課題文に参考リンクが多い）。
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    md.use(
        dollarmath_plugin,
        renderer=_math_renderer,
        # `$100 と $200` を数式と読ませない。金額は課題文に普通に出る。
        allow_space=False,
        double_inline=True,
    )
    return md


def _math_renderer(content: str, options: dict[str, object]) -> str:
    """LaTeX の断片を MathML にする。失敗したら元の文字列を出す。

    `options` は `dollarmath` が渡す描画時の情報で、`display_mode` が
    `$$...$$`（別行立て）かどうかを表す。
    """
    import html

    display = bool(options.get("display_mode"))
    try:
        from latex2mathml.converter import convert

        return convert(content, display="block" if display else "inline")
    except Exception:
        marker = "$$" if display else "$"
        return f"<code>{html.escape(marker + content + marker)}</code>"


def render_statement(markdown: str) -> str:
    """課題文を HTML にする。

    描画できない場合でも例外にしない。課題文が読めないより、生の Markdown が
    見えている方がまし（提出はできる）。
    """
    try:
        return _renderer().render(markdown)
    except Exception:
        import html

        return f"<pre>{html.escape(markdown)}</pre>"
