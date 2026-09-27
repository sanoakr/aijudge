"""カテゴリの見出しが、中の項目と見分けられること（2026-09-27）。

設定の一覧のカテゴリ（`.settings-group`：日程・提出・見せる相手…）は、以前 .78rem の
灰色で、太字の項目名（`.setting-name`・.88rem）より弱かった。課題の内容の大項目
（`.taskform > h2`：問題・採点・分類）は、中の項目の見出しと同じ大きさの太字だった。
どちらも「どこからが次のカテゴリか」が読めなかった。

固定するのは、左のレールの大項目（`.railgrp .gh`）と同じ書き方であること ──
**項目名より字を小さくしない**、**地の色を持つ**（帯）、**文字色に藍を使わない**
（藍は「押して移る」の色）。
"""

from __future__ import annotations

import re

import aijudge_webui as webui

CONSOLE_CSS = (webui.ASSETS_DIR / "console.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", CONSOLE_CSS)
    assert match, f"{selector} の規則が無い"
    return match.group(1)


def _rem(rule: str) -> float:
    match = re.search(r"font-size:([\d.]+)rem", rule)
    assert match, "font-size が rem で書かれていない"
    return float(match.group(1))


def test_a_category_is_not_smaller_than_the_items_under_it() -> None:
    assert _rem(_rule(".settings-group")) >= _rem(_rule(".setting-name"))


def test_categories_are_bands() -> None:
    for selector in (".settings-group", ".taskform > h2"):
        rule = _rule(selector)
        assert "background:var(--ai-soft)" in rule, selector
        assert "color:var(--ink)" in rule, selector
        assert "color:var(--muted)" not in rule, selector
        assert "color:var(--ai)" not in rule, f"{selector} の文字色に藍を使っている"
