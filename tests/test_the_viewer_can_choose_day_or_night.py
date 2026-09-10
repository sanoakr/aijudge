"""配色の切り替えが両アプリに同じ形で入っていること（#181）。

ブラウザでの実際の切り替わりは手で確認した（`data-theme` と
`prefers-color-scheme` の組み合わせ 4 通り、JavaScript 有無）。ここで
機械が守るのは、**壊れても画面を見ただけでは気づけない性質**の方である。

- 夜の値が 2 か所（OS 追従と明示の選択）に**同じだけ**あること
  ── 片方だけ直すと、自分で夜を選んだときと OS が夜のときで色が違う
- OS 追従が明示の「昼」に負けること（`:not([data-theme="light"])`）
- JavaScript が無いときにスイッチを出さないこと
- **両アプリで同じであること** ── 2 つの base.html は配色部分が
  同一で、片方だけ直る事故がいちばん起きやすい
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "learner": REPO_ROOT / "apps/studentweb/src/aijudge_studentweb/templates/base.html",
    "console": REPO_ROOT / "apps/reviewconsole/src/aijudge_reviewconsole/templates/base.html",
}

# 夜の配色を代表する値。ここが変わったらテストの方も直す（値そのものを
# 固定したいのではなく、2 か所が一致していることを見たい）。
NIGHT_PAPER = "--paper:#0e141c"


@pytest.fixture(params=sorted(TEMPLATES), ids=sorted(TEMPLATES))
def template(request) -> str:
    return TEMPLATES[request.param].read_text(encoding="utf-8")


def test_the_night_palette_is_defined_for_both_ways_of_asking(template: str) -> None:
    """OS 追従と明示の選択の両方に、同じ夜の値があること。

    CSS では変数を 1 か所にまとめられないので 2 度書くしかない。
    **片方だけ直すと気づきにくい形で壊れる** ── 自分で夜を選んだ人と、
    OS が夜の人とで、別の色を見ることになる。
    """
    assert template.count(NIGHT_PAPER) == 2, (
        '夜の配色は OS 追従（@media）と明示の選択（[data-theme="dark"]）の '
        "2 か所に要る。1 か所しかないなら、どちらかの経路で昼のまま出ている"
    )
    assert "@media (prefers-color-scheme: dark)" in template
    assert ':root[data-theme="dark"]' in template


def test_an_explicit_daytime_choice_beats_the_os(template: str) -> None:
    """OS が夜でも「昼」を選べること。

    `@media` の中を素の `:root` にすると、明示の選択が同じ詳細度で
    先に書かれている側に負け、**選んでも戻らない**。
    """
    guarded = ':root:not([data-theme="light"])'
    assert guarded in template, f"{guarded} が無い。OS が夜の端末で「昼」を選んでも戻らない"


def test_the_switch_is_not_shown_without_javascript(template: str) -> None:
    """押しても何も起きないボタンを見せない（この画面群の作法）。

    隠し方は既存の `.js-only` に合わせる。同じ目的の仕組みを 2 つ持つと、
    片方を直したときにもう片方が取り残される。
    """
    switch = re.search(r'<div class="theme-switch[^"]*"[^>]*>', template)
    assert switch is not None, "配色スイッチが base.html に無い"
    assert "js-only" in switch.group(0)
    assert "hidden" in switch.group(0)
    assert 'document.querySelectorAll(".js-only")' in template, (
        ".js-only を外すスクリプトが無いと、スイッチは永久に隠れたまま"
    )


def test_the_choice_is_applied_before_the_first_paint(template: str) -> None:
    """`</head>` より前で当てること。

    body の下に置くと、昼で 1 枚描いてから夜へ切り替わるのが見える。
    """
    head_end = template.index("</head>")
    assert template.index('localStorage.getItem("aijudge-theme")') < head_end, (
        "配色の適用が head より後にある。最初の描画で昼が一瞬見える"
    )


def test_reading_the_stored_choice_cannot_break_the_page(template: str) -> None:
    """`localStorage` は読むだけで例外を投げることがある。

    プライベートウィンドウやサイトデータを禁じた設定で、**画面全体が
    白紙になる**のがいちばん困る壊れ方である。
    """
    head = template[: template.index("</head>")]
    assert "try{" in head and "catch" in head, (
        "head のスクリプトが localStorage を素で読んでいる。"
        "サイトデータを禁じた環境で例外になり、以降のスクリプトが止まる"
    )


def test_three_choices_because_dropping_auto_would_be_a_regression() -> None:
    """`auto` を捨てない。

    いま何も設定せずに夜で見えている人が、捨てた瞬間に昼へ戻る。
    切り替えを足すことが、その人にとっての機能削除になってはいけない。
    """
    for name, path in TEMPLATES.items():
        text = path.read_text(encoding="utf-8")
        choices = set(re.findall(r'data-theme-choice="(\w+)"', text))
        assert choices == {"auto", "light", "dark"}, f"{name}: {choices}"


def test_both_apps_carry_the_same_palette_and_switch() -> None:
    """2 つの base.html は配色部分が同一である。

    片方だけ直る事故がいちばん起きやすいので、機械で突き合わせる。
    """
    blocks = {}
    for name, path in TEMPLATES.items():
        text = path.read_text(encoding="utf-8")
        start = text.index(":root{")
        end = text.index("*{box-sizing:border-box}")
        blocks[name] = text[start:end]
    learner, console = blocks["learner"], blocks["console"]
    assert learner == console, "学習者アプリとレビューコンソールで配色が食い違っている"
