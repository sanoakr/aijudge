"""参照している CSS 変数が、どれも定義されていること（#215）。

**この取りこぼしは目で見つからない。** `var(--x)` の参照先が無く代替値も
無い宣言は、警告も出さずに計算時に無効となり、規則ごと落ちる ── 書いた
本人の画面でも「なぜか効かない」としか見えない。実際に 2 つ落ちていた。

    .demo-banner{margin-bottom:var(--s5)}   尺度が存在せず、余白が広がらない
    .rail a.nav .c{font-family:var(--font-mono)}  等幅にならず、桁が揃わない

**代替値つき（`var(--x, 12px)`）は正当なので数えない。** 値を持たない環境が
あることを承知で書く形であり、落ちる書き方ではない。
"""

from __future__ import annotations

import re

import aijudge_webui as webui

#: `--name:` の形で値を与えている箇所。
DEFINITION = re.compile(r"(--[a-z0-9-]+)\s*:")
#: `var(--name)` / `var(--name, 代替値)`。2 つ目の群が空なら代替値は無い。
REFERENCE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*([^)]*)\)")
#: 注記。**この CSS の注記は理由を書くので長く、変数名を引用する** ── 剥がさずに
#: 走査すると、直したはずの名前が散文の中に残っていて偽陽性になる。
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _sheets() -> dict[str, str]:
    return {
        path.name: COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        for path in webui.ASSETS_DIR.glob("*.css")
    }


def test_every_variable_used_without_a_fallback_is_defined() -> None:
    sheets = _sheets()
    # **定義は 1 枚に閉じない。** `learner.css` / `console.css` は `base.css` の
    # あとに読まれる前提で、同じ根から引く（`base.css` 冒頭の注記）。
    defined = {name for text in sheets.values() for name in DEFINITION.findall(text)}

    missing: list[str] = []
    for filename, text in sheets.items():
        for name, fallback in REFERENCE.findall(text):
            if fallback.strip():
                continue
            if name not in defined:
                missing.append(f"{filename}: var({name})")

    assert not missing, "定義の無い変数を参照しています: " + ", ".join(sorted(missing))
