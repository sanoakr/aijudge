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

# 数式は当面そのまま出す。KaTeX を入れるのは手書き数式（Phase 4）と同時が
# 妥当で、いま入れると課題文の描画だけのために依存が増える。
# `$$...$$` はコードブロック外ではそのまま表示される。


@lru_cache(maxsize=1)
def _renderer():
    from markdown_it import MarkdownIt

    # `html=False` が要点。課題文に埋め込まれた HTML をそのまま出さない。
    # `linkify` は URL を自動リンクにする（課題文に参考リンクが多い）。
    return MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})


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
