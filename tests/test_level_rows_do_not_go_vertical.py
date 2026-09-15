"""段階の並びが、狭い幅で縦書きにならない。

判定の画面（`reveal.html`）と blind 採点の画面（`blind.html`）は、観点ごとの
段階を 1 行 1 段階で並べる。行は flex で、狭いと最後に縮むのは説明文である
── **日本語の min-content は 1 文字**なので、説明が幅 14px・高さ 271px の
縦書きになっていた（実測）。判定の画面は 2 面で右の面は画面の半分しかなく、
いちばん起きるのは**選ばれている段階**である ── そこにだけ「システム判定」
「あなたの blind 採点」の札が並んで幅を奪う。ガイドの画像にもその姿が
残っていた（`docs/guide/images/ta-reveal-form.png`）。

直したのは CSS（折り返させ、説明文に下限を与える）だが、**CSS が名指しする
ための名前がテンプレート側に要る**。ここで固定するのはその結び目である ──
名前が外れると、CSS は静かに効かなくなり、崩れは画面を開くまで分からない。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates"
STYLESHEET = REPO_ROOT / "packages" / "webui" / "src" / "aijudge_webui" / "assets" / "base.css"
LEVEL_SCREENS = (CONSOLE / "reveal.html", CONSOLE / "blind.html")


def test_every_level_descriptor_is_named() -> None:
    """説明文は無名の `<span>` ではない。**縮む相手を CSS が名指しできる。**"""
    for path in LEVEL_SCREENS:
        body = path.read_text(encoding="utf-8")
        assert "{{ level.descriptor }}" in body, f"{path.name}: 段階の説明が消えている"
        assert re.search(r'<span class="lv-text">\{\{ level\.descriptor \}\}</span>', body), (
            f"{path.name}: 段階の説明に lv-text が付いていない（狭い幅で縦書きになる）"
        )


def test_the_level_row_wraps_instead_of_shrinking() -> None:
    """行は**折り返す**。縮めると、日本語は 1 文字ずつの縦並びになる。"""
    css = STYLESHEET.read_text(encoding="utf-8")
    row = css.split(".levels label{")[1].split("}")[0]
    assert "flex-wrap:wrap" in row, "段階の行が折り返さない"
    assert ".lv-text{" in css, "説明文の規則が無い"
    text = css.split(".lv-text{")[1].split("}")[0]
    # 下限が無いと、折り返す前に 1 文字幅まで縮む余地が残る。
    assert "min-width" in text, "説明文に幅の下限が無い"
