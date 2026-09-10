"""画面の見た目が 1 か所にしか無いこと（#184）。

以前は `apps/studentweb` と `apps/reviewconsole` の `base.html` が `<style>`
として同じ CSS を丸ごと 2 部持っていた ── 239 行と 410 行のうち **221 行が
重複**し、配色の定義に至っては 55 行が完全に一致していた。そのコメント自身
が「片方だけ直すと気づきにくい壊れ方をする」と警告していたが、警告は仕組み
ではない。

**「重複させない」という規約ではなく、重複したら落ちるテストにする**
（このリポジトリの作法・`.importlinter` と同じ考え方）。ここで固定するのは
「見た目の定義がどこにあるか」だけで、見た目そのものではない。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI = REPO_ROOT / "packages/webui/src/aijudge_webui"
BASE_CSS = WEBUI / "assets/base.css"
LEARNER_CSS = WEBUI / "assets/learner.css"
CONSOLE_CSS = WEBUI / "assets/console.css"
TEMPLATES = {
    "learner": REPO_ROOT / "apps/studentweb/src/aijudge_studentweb/templates/base.html",
    "console": REPO_ROOT / "apps/reviewconsole/src/aijudge_reviewconsole/templates/base.html",
}
# 配色の変数。**1 つでも `base.html` に戻っていたら落とす。** 代表値ではなく
# 定義そのもの（`--名前:`）を見る ── 値は変わってよいが、置き場所は変わらない。
PALETTE_TOKENS = ("--paper:", "--ink:", "--shu:", "--kin:", "--verd:", "--line:")


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_no_template_carries_its_own_stylesheet(name: str) -> None:
    """`base.html` に `<style>` を戻さない。

    戻した瞬間、そこに書いた規則は**もう片方のアプリには入らない**。
    2 つの画面が別の見た目になり始める入口がここだった。
    """
    text = TEMPLATES[name].read_text(encoding="utf-8")
    body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)  # Jinja のコメントは対象外
    assert "<style" not in body, (
        f"{name}: base.html に `<style>` が戻っている。見た目は packages/webui に置くこと（#184）"
    )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_palette_is_defined_in_exactly_one_place(name: str) -> None:
    """配色の定義が `base.html` に無いこと。

    重複していた 55 行のうち、いちばん静かに壊れるのがここ ── 片方だけ
    直すと、学習者と教員が別の色を見る。
    """
    text = TEMPLATES[name].read_text(encoding="utf-8")
    body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    leaked = [token for token in PALETTE_TOKENS if token in body]
    assert not leaked, f"{name}: 配色の定義が base.html に戻っている: {leaked}"


def test_the_palette_is_actually_in_the_shared_stylesheet() -> None:
    """裏返し。**「どこにも無い」を「1 か所にある」と読み違えない。**

    上の 2 つは不在だけを見るので、共有側ごと消えても通ってしまう。
    """
    css = BASE_CSS.read_text(encoding="utf-8")
    for token in PALETTE_TOKENS:
        assert token in css, f"{token} が共有の CSS に無い"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_both_apps_link_the_shared_stylesheet_with_a_version(name: str) -> None:
    """`static_url()` 経由で読むこと。

    版を付けないと、配置し直したのにブラウザが前の CSS を使い続け、症状は
    「配置に失敗した」ように見える。接頭辞（`AIJUDGE_CONSOLE_ROOT_PREFIX`）を
    通すのも `static_url()` の仕事で、直書きすると `/console` の下で 404 に
    なる ── 同じ忘れ方を一度出荷している（#165）。
    """
    text = TEMPLATES[name].read_text(encoding="utf-8")
    assert "{{ static_url('base.css') }}" in text, f"{name}: 共有の CSS を読んでいない"
    assert not re.search(r'href="/static/', text), (
        f"{name}: CSS の URL を直書きしている。static_url() を通すこと"
    )


def test_the_console_reads_its_own_sheet_after_the_shared_one() -> None:
    """`console.css` は `base.css` の**あと**に読む。

    足す側なので、順を変えると足したはずの規則が先に読まれる。
    """
    text = TEMPLATES["console"].read_text(encoding="utf-8")
    base = text.index("{{ static_url('base.css') }}")
    console = text.index("{{ static_url('console.css') }}")
    assert base < console, "console.css が base.css より先に読まれている"


def test_the_learner_app_reads_its_own_sheet_after_the_shared_one() -> None:
    """`learner.css` も `base.css` の**あと**に読む（同じ理由）。"""
    text = TEMPLATES["learner"].read_text(encoding="utf-8")
    assert text.index("{{ static_url('base.css') }}") < text.index(
        "{{ static_url('learner.css') }}"
    ), "learner.css が base.css より先に読まれている"


def test_neither_app_reads_the_other_app_s_sheet() -> None:
    """片方の画面にしか出ない部品を、もう片方に配らない。

    配信量の話ではない。**当たる規則を切り出し前と同じに保つため**である ──
    学習者側の `.inline-field .body input` はチェックボックスを除外して
    いないので、コンソールに配ると `width:100%` がチェックボックスに効く。
    """
    assert "console.css" not in TEMPLATES["learner"].read_text(encoding="utf-8")
    assert "learner.css" not in TEMPLATES["console"].read_text(encoding="utf-8")


def test_no_sheet_repeats_a_rule_from_the_shared_one() -> None:
    """アプリ固有の 2 枚が、共有分の規則を重複して持たないこと。"""

    def sigs(path: Path) -> set[str]:
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        return {re.sub(r"\s+", " ", m).strip() for m in re.findall(r"[^{}]*\{[^{}]*\}", text)}

    shared = sigs(BASE_CSS)
    for path in (LEARNER_CSS, CONSOLE_CSS):
        both = sorted(shared & sigs(path))
        assert not both, f"{path.name} が base.css と同じ規則を持っている: {both}"


def test_the_two_sheets_share_no_selector() -> None:
    """同じセレクタを 2 つのファイルに書かないこと。

    `console.css` は `base.css` の**あと**に読まれるので、同じセレクタを
    書くとそのぶんだけ黙って勝つ ── 共有側を直したのに効かない、という
    追いにくい壊れ方になる。切り出した時点では 0 件だった。
    """

    def selectors(css: str) -> set[str]:
        text = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        return {s.strip() for s in re.findall(r"(?m)^([^\s@}][^{]*)\{", text)}

    shared = selectors(BASE_CSS.read_text(encoding="utf-8"))
    console = selectors(CONSOLE_CSS.read_text(encoding="utf-8"))
    both = sorted(shared & console)
    assert not both, f"base.css と console.css に同じセレクタがある: {both}"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_theme_switch_is_read_from_the_shared_partial(name: str) -> None:
    """配色の切り替えも 1 か所から読むこと。

    以前は 14 行の先読みと 32 行の切り替えが**両アプリに同じ形で**あった
    （合わせて 4 か所）。仕掛けを 2 部持つと、片方だけ直る。
    """
    text = TEMPLATES[name].read_text(encoding="utf-8")
    assert '{% include "_theme_boot.html" %}' in text
    assert '{% include "_theme_switch.html" %}' in text
    assert "data-theme-choice" not in text, f"{name}: 切り替えの部品が base.html に戻っている"
    assert 'localStorage.setItem("aijudge-theme"' not in text
