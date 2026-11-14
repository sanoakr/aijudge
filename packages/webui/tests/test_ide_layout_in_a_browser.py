"""エディタの画面の並べ方（`.ide-panel[data-layout]`）を実ブラウザで確かめる（2026-11-15）。

固定したいのは 3 つ（と、2026-11-15 の入出力の位置・境界の大きさ）。

左に置くと問題文とエディタが横に並ぶ   広い画面（1100px 以上）で、問題文がエディタの左にある
上に置くと従来どおり積む               問題文がエディタの上にある（狭い画面は属性が何でも積む）
隠すとエディタが広がる                 問題文が消え、エディタが左端から始まる

アンケート（network・2026-11-13）で「問題文とエディタの行き来にスクロールが要る」という声が
多かった。CSS の効き方だけを見るので、Monaco や ide.js は読み込まない（ボタンの動きは
`ide.js`、見た目は `learner.css`）。

Playwright とブラウザが無い環境では skip する（`AIJUDGE_REQUIRE_BROWSER=1` なら失敗）。
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

from aijudge_webui import ASSETS_DIR

REQUIRED = os.environ.get("AIJUDGE_REQUIRE_BROWSER") == "1"


def _unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(f"AIJUDGE_REQUIRE_BROWSER=1 but {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


try:
    import playwright.sync_api as playwright_sync
except ImportError as error:
    _unavailable(f"could not import playwright.sync_api: {error}")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>
<main><div class="ide-sticky"><div class="ide-head"><h1>exam</h1></div></div>
<section class="ide-panel" id="panel" data-layout="side" data-statement="shown">
<div class="ide-statement"><div class="statement">{statement}</div></div>
<div class="ide-split ide-split-statement" data-split="statement"></div>
<div class="ide-work"><div class="ide-main"><div class="ide-editor"></div>
<div class="ide-split ide-split-editor" data-split="editor"></div>
<aside class="ide-side"><div class="ide-runbox"><div class="ide-stdin"><textarea></textarea></div>
<div class="ide-output"><pre class="ide-io"></pre></div></div>
<div class="ide-actions"><button>実行</button><button>提出</button></div></aside>
</div></div></section></main></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as error:  # ブラウザが無いだけ
            _unavailable(f"chromium is not installed: {error}")
        yield instance
        instance.close()


def _boxes(
    browser, *, width: int, layout: str, statement: str = "shown", root_style: str = ""
) -> dict[str, dict]:
    css = "".join(
        (ASSETS_DIR / name).read_text(encoding="utf-8") for name in ("base.css", "learner.css")
    )
    page = browser.new_page(viewport={"width": width, "height": 800})
    page.set_content(PAGE.format(css=css, statement="<p>問題文</p>" * 30))
    if root_style:
        page.evaluate("(t) => document.documentElement.style.cssText = t", root_style)
    page.evaluate(
        "([l, s]) => { const p = document.getElementById('panel');"
        " p.dataset.layout = l; p.dataset.statement = s; }",
        [layout, statement],
    )
    out = {}
    for name, selector in (
        ("statement", ".ide-statement"),
        ("editor", ".ide-editor"),
        ("split_statement", ".ide-split-statement"),
        ("split_editor", ".ide-split-editor"),
        ("runbox", ".ide-runbox"),
        ("actions", ".ide-actions"),
    ):
        out[name] = page.eval_on_selector(
            selector,
            "e => { const r = e.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height,"
            " shown: r.width > 0 && r.height > 0}; }",
        )
    page.close()
    return out


def test_the_statement_sits_left_of_the_editor_on_a_wide_screen(browser) -> None:
    boxes = _boxes(browser, width=1280, layout="side")

    assert boxes["statement"]["x"] + boxes["statement"]["w"] <= boxes["editor"]["x"]
    # 同じ高さから始まる（上下に積んでいない）。
    assert abs(boxes["statement"]["y"] - boxes["editor"]["y"]) < 60


def test_the_statement_sits_above_the_editor_when_stacked(browser) -> None:
    boxes = _boxes(browser, width=1280, layout="stack")

    assert boxes["statement"]["y"] + boxes["statement"]["h"] <= boxes["editor"]["y"] + 1


def test_a_narrow_screen_stacks_whatever_the_setting(browser) -> None:
    """半分の幅のウィンドウ（学生はよく画面を左右に分ける）で、横並びにしない。"""
    boxes = _boxes(browser, width=700, layout="side")

    assert boxes["statement"]["y"] + boxes["statement"]["h"] <= boxes["editor"]["y"] + 1


def test_hiding_the_statement_gives_the_editor_the_whole_row(browser) -> None:
    shown = _boxes(browser, width=1280, layout="side")
    hidden = _boxes(browser, width=1280, layout="side", statement="hidden")

    assert hidden["statement"]["shown"] is False
    assert hidden["editor"]["x"] < shown["editor"]["x"]
    assert hidden["editor"]["w"] > shown["editor"]["w"]


def test_the_run_tools_sit_below_the_editor(browser) -> None:
    """入出力と実行・提出ボタンはエディタの下（右ではない）。"""
    for layout, width in (("side", 1280), ("stack", 1280), ("side", 700)):
        boxes = _boxes(browser, width=width, layout=layout)

        assert boxes["editor"]["y"] + boxes["editor"]["h"] <= boxes["split_editor"]["y"] + 1
        assert boxes["split_editor"]["y"] + boxes["split_editor"]["h"] <= boxes["runbox"]["y"] + 1
        assert boxes["runbox"]["y"] + boxes["runbox"]["h"] <= boxes["actions"]["y"] + 1
        # 同じ幅の列に載っている（エディタの右に回り込んでいない）。
        assert abs(boxes["runbox"]["x"] - boxes["editor"]["x"]) < 1


def test_the_editor_height_follows_the_variable(browser) -> None:
    small = _boxes(browser, width=1280, layout="side", root_style="--ide-editor-h:200px")
    large = _boxes(browser, width=1280, layout="side", root_style="--ide-editor-h:500px")

    assert round(small["editor"]["h"]) == 200
    assert round(large["editor"]["h"]) == 500


def test_the_statement_width_follows_the_variable_when_side_by_side(browser) -> None:
    narrow = _boxes(browser, width=1280, layout="side", root_style="--ide-statement-w:300px")
    wide = _boxes(browser, width=1280, layout="side", root_style="--ide-statement-w:600px")

    assert round(narrow["statement"]["w"]) == 300
    assert round(wide["statement"]["w"]) == 600
    # つかみ手は問題文とエディタの間にある。
    assert wide["statement"]["x"] + wide["statement"]["w"] <= wide["split_statement"]["x"] + 1
    assert wide["split_statement"]["x"] + wide["split_statement"]["w"] <= wide["editor"]["x"] + 1


def test_the_statement_width_is_kept_within_bounds(browser) -> None:
    """保存した幅が今の画面に合わなくても（広い画面で決めた値を狭い画面で開く）、はみ出さない。"""
    tiny = _boxes(browser, width=1280, layout="side", root_style="--ide-statement-w:10px")
    huge = _boxes(browser, width=1280, layout="side", root_style="--ide-statement-w:5000px")

    assert tiny["statement"]["w"] >= 200
    assert huge["statement"]["w"] <= 1280 * 0.7 + 1
    assert huge["editor"]["w"] > 200


def test_the_statement_height_follows_the_variable_when_stacked(browser) -> None:
    short = _boxes(browser, width=1280, layout="stack", root_style="--ide-statement-h:120px")
    tall = _boxes(browser, width=1280, layout="stack", root_style="--ide-statement-h:400px")

    assert round(short["statement"]["h"]) == 120
    assert round(tall["statement"]["h"]) == 400
    # 上下に積むときのつかみ手は、問題文とエディタの間の横の帯。
    assert tall["statement"]["y"] + tall["statement"]["h"] <= tall["split_statement"]["y"] + 1
    assert tall["split_statement"]["y"] + tall["split_statement"]["h"] <= tall["editor"]["y"] + 1
