"""教員・モデルが書いた Markdown の描画。

課題文は Markdown である（取り込み器が `desc.md` を読む）。**その形式を
決めているのがこのパッケージなので、安全に描画する方法も同じ場所に置く。**
表示側のアプリ 2 つ（学習者向けと教員向け）が別々に設定を持つと、片方で
安全側の設定が抜ける。

**課題文以外にも同じ描画を使う。** コースの基本情報（`Course.description`、
シラバスの概要・到達目標）も同じ性質を持つ ── Markdown で、教員かモデルが
書き、画面に出る。別の描画を持たせると、上の「片方で安全側の設定が抜ける」
がそのまま起きる。描画器は 1 つにして、入口の名前だけを分ける。

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
    from mdit_py_plugins.attrs import attrs_plugin
    from mdit_py_plugins.dollarmath import dollarmath_plugin

    # `html=False` が要点。課題文に埋め込まれた HTML をそのまま出さない。
    # `linkify` は URL を自動リンクにする（課題文に参考リンクが多い）。
    #
    # **`breaks=True`。単一の改行をそのまま改行として出す。** CommonMark の
    # 既定は段落内の改行を空白に潰すが、日本語で書かれた本文は改行を
    # 意味の切れ目として使う ── 実測（2026-08-30）でシラバスの授業計画が
    # 1 つの段落に潰れ、第 1 回から第 15 回までが繋がって出た。
    #
    # 課題文への影響は無い。取り込み対象の `desc.md` は段落内で改行せず
    # （空行で段落を分ける）書かれており、潰れる改行がそもそも無い。
    md = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": True, "typographer": False, "breaks": True},
    )
    # 画像の表示幅（`![](url){width=480}`）。**画像の直後の `{...}` だけ、
    # しかも `width` だけを通す。**
    #
    # 縮めずに貼ると写真 1 枚で画面が埋まり、課題文の続きが画面外へ出る
    # （`images.DISPLAY_WIDTH`）。かといって `html=False` を緩めて
    # `<img width=...>` を書かせるわけにはいかない ── Phase 4 の AI 作問で
    # 課題文はモデルの出力になるので、生の HTML が通る経路を作らない。
    #
    # **高さは通さない。** 幅だけなら縦横比は描画側が保つ（CSS の
    # `height:auto`）。両方書けると、幅だけ直したときに絵が歪む。
    md.use(
        attrs_plugin,
        after=("image",),
        allowed=("width",),
    )
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


def render_markdown(markdown: str) -> str:
    """Markdown を HTML にする。生の HTML は通さず、数式は MathML にする。

    描画できない場合でも例外にしない。読めないより生の Markdown が見えている
    方がまし（課題文なら提出はできるし、シラバスなら読める）。
    """
    try:
        return _renderer().render(markdown)
    except Exception:
        import html

        return f"<pre>{html.escape(markdown)}</pre>"


def render_statement(markdown: str) -> str:
    """課題文を HTML にする。描画そのものは `render_markdown` と同じ。

    **名前を分けてあるのは呼び出し側のため。** 課題文を出しているのか
    シラバスを出しているのかが、呼んでいる場所を読めば分かるようにしてある。
    """
    return render_markdown(markdown)
