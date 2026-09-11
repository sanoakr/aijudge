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
from pathlib import Path

import aijudge_webui as webui

#: 画面の置き場所。**共有のぶんと両アプリのぶん**を見る ── どれも同じ
#: `base.css` の上で描かれるので、落ち方は 1 つである。
_REPO = Path(__file__).resolve().parents[3]
TEMPLATE_ROOTS = (
    webui.TEMPLATES_DIR,
    _REPO / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "templates",
    _REPO / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates",
)

#: `--name:` の形で値を与えている箇所。**大文字と下線も名前に使える**
#: （カスタムプロパティは大小を区別する）ので、小文字だけを見ない。
DEFINITION = re.compile(r"(--[\w-]+)\s*:")
#: `var(--name)` / `var(--name, 代替値)`。2 つ目の群が空なら代替値は無い。
#: **入れ子（`var(--a, var(--b))`）も内側まで数える**ため、`)` で止めずに
#: 名前だけを拾い、代替値の有無は直後が `,` かどうかで見る。
REFERENCE = re.compile(r"var\(\s*(--[\w-]+)\s*(,?)")
#: 注記。**この CSS の注記は理由を書くので長く、変数名を引用する** ── 剥がさずに
#: 走査すると、直したはずの名前が散文の中に残っていて偽陽性になる。
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


#: どの頁がどの順で読むか（`base.html` の `<link>`）。**`console.css` は
#: `learner.css` を読まない**ので、定義の集合を 3 枚の和にすると「学習者側で
#: 定義し、コンソール側で参照する」取りこぼしを見逃す。
BUNDLES = {
    "学習者": ("base.css", "learner.css"),
    "コンソール": ("base.css", "console.css"),
}


def _sheets() -> dict[str, str]:
    return {
        path.name: COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        for path in webui.ASSETS_DIR.glob("*.css")
    }


def test_every_variable_used_without_a_fallback_is_defined() -> None:
    """**実際に一緒に読まれる組ごとに確かめる。**

    3 枚を一括りにすると、片方の頁にしか定義が無い変数を「定義済み」と
    数えてしまう ── #215 で落ちていたのと同じ形のまま、テストだけが通る。
    """
    sheets = _sheets()
    missing: list[str] = []
    for bundle, names in BUNDLES.items():
        defined = {name for filename in names for name in DEFINITION.findall(sheets[filename])}
        for filename in names:
            for name, comma in REFERENCE.findall(sheets[filename]):
                if comma:
                    continue
                if name not in defined:
                    missing.append(f"{bundle}／{filename}: var({name})")

    assert not missing, "定義の無い変数を参照しています: " + ", ".join(sorted(set(missing)))


def test_the_templates_do_not_reach_for_a_variable_that_is_not_there() -> None:
    """**画面の中の `style=\"…\"` も同じ落ち方をする。**

    共有テンプレートにも `style="color:var(--muted)"` の形があり、CSS の
    ファイルだけを見ていると、そこに書いた未定義の名前は素通りする。
    """
    sheets = _sheets()
    defined = {name for text in sheets.values() for name in DEFINITION.findall(text)}

    missing: list[str] = []
    for path in sorted(TEMPLATE_ROOTS):
        for template in path.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            for name, comma in REFERENCE.findall(text):
                if comma or name in defined:
                    continue
                missing.append(f"{template.name}: var({name})")

    assert not missing, "定義の無い変数を参照しています: " + ", ".join(sorted(set(missing)))
