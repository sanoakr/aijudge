"""課題文の描画の規則を固定する。

固定したいのは 3 つ。

読める    Markdown はレンダリングする。生の `##` や ``` が出ていると、
          コードブロックが本体のプログラミング課題は読めない。
安全      生の HTML は通さない。課題文は AI 作問（Phase 4）でモデルの
          出力になるので、`<script>` が通る経路を最初から塞ぐ。
数式      `$...$` は MathML にする。**サーバ側で変換する** ── CDN の
          KaTeX に頼ると、課題文が読めるかが外向きの通信に依存する。
"""

from __future__ import annotations

from aijudge_authoring import render_statement


def test_markdown_is_rendered() -> None:
    html = render_statement("## 見出し ##\n\n```c\nint main(void);\n```\n")
    assert "<h2>" in html
    assert "<code" in html
    assert "```" not in html


def test_raw_html_does_not_pass_through() -> None:
    """課題文はいずれモデルの出力になる。`<script>` の経路を塞いでおく。"""
    html = render_statement("<script>alert(1)</script>\n")
    assert "<script>" not in html


def test_inline_math_becomes_mathml() -> None:
    html = render_statement(r"平均は $\bar{x}$ である。")
    assert "<math" in html
    assert 'display="inline"' in html
    assert r"\bar" not in html


def test_display_math_becomes_a_block() -> None:
    html = render_statement("$$\nE = mc^2\n$$\n")
    assert 'display="block"' in html
    assert "math block" in html


def test_a_bare_dollar_amount_is_not_math() -> None:
    """金額は課題文に普通に出る。`$100 と $200` を数式と読ませない。"""
    html = render_statement("参加費は $100 と $200 です。")
    assert "<math" not in html
    assert "$100" in html


def test_broken_math_falls_back_to_the_source() -> None:
    """課題文が読めないより、元の式が見えている方がまし（提出はできる）。"""
    html = render_statement(r"$\thisisnotacommand{{{$")
    assert "<h1" not in html
    # 例外にしない。何らかの形で本文が残っていること。
    assert html.strip()
