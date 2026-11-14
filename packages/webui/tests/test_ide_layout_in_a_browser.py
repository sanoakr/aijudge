"""エディタの画面の並べ方（`.ide-panel[data-layout]`）を実ブラウザで確かめる（2026-11-15）。

固定したいのは 3 つ。

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
<div class="ide-work"><div class="ide-main"><div class="ide-editor"></div></div>
<aside class="ide-side"><div class="ide-actions">
<button>実行</button><button>提出</button></div></aside>
</div></section></main></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as error:  # ブラウザが無いだけ
            _unavailable(f"chromium is not installed: {error}")
        yield instance
        instance.close()


def _boxes(browser, *, width: int, layout: str, statement: str = "shown") -> dict[str, dict]:
    css = "".join(
        (ASSETS_DIR / name).read_text(encoding="utf-8") for name in ("base.css", "learner.css")
    )
    page = browser.new_page(viewport={"width": width, "height": 800})
    page.set_content(PAGE.format(css=css, statement="<p>問題文</p>" * 30))
    page.evaluate(
        "([l, s]) => { const p = document.getElementById('panel');"
        " p.dataset.layout = l; p.dataset.statement = s; }",
        [layout, statement],
    )
    out = {}
    for name, selector in (("statement", ".ide-statement"), ("editor", ".ide-editor")):
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
