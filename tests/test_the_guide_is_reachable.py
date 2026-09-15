"""利用ガイドへの導線（#327）。

**どの画面からも届く。** 使い方が分からなくなるのは作業の途中であって、
入口に戻ってから探すものではない ── だから帯（ヘッダ）に置く。署名が無い
と帯は出ないので、**入口にも置く**：ログインの前に読みたい人にはそこしか
経路が無い。

置き場所は 6 つ（帯 2 つ + 入口 4 つ）。URL は `aijudge_webui.GUIDE_URL` に
1 つだけ持つ ── 書き写すと、出す場所を増やした日にどれかが古いままになる。
"""

from __future__ import annotations

import re
from pathlib import Path

from aijudge_webui import GUIDE_URL, guide_url

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "templates"
LEARNER = REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "templates"

HEADERS = (CONSOLE / "base.html", LEARNER / "base.html")
ENTRANCES = (
    CONSOLE / "login.html",
    CONSOLE / "login_local.html",
    LEARNER / "login.html",
    LEARNER / "login_local.html",
)


def test_the_guide_url_matches_the_published_site() -> None:
    """`mkdocs.yml` の `site_url` と同じ場所を指すこと。

    ここがずれると、画面は案内を出しているのに誰も読めない頁へ送る ──
    **壊れていることが画面からは分からない**種類の誤りである。
    """
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    published = re.search(r"^site_url:\s*(\S+)\s*$", mkdocs, flags=re.MULTILINE)
    assert published is not None, "mkdocs.yml に site_url が無い"
    assert published.group(1) == GUIDE_URL


def test_the_guide_link_is_in_both_headers() -> None:
    """帯は**どの画面にも出る**ので、ここに 1 つあれば全画面から届く。"""
    for path in HEADERS:
        body = path.read_text(encoding="utf-8")
        assert "{{ guide_url() }}" in body, f"{path.parent.parent.name}: 帯に利用ガイドが無い"


def test_the_guide_link_is_on_every_entrance() -> None:
    """**4 つの入口すべて。** 署名が無い人には、ここしか経路が無い。"""
    for path in ENTRANCES:
        body = path.read_text(encoding="utf-8")
        assert "{{ guide_url() }}" in body, f"{path.name}: 入口に利用ガイドが無い"


def test_no_template_writes_the_address_by_hand() -> None:
    """**URL は 1 か所**（`aijudge_webui.GUIDE_URL`）。

    書き写した場所は、公開先が変わった日に黙って古いままになる。
    """
    for path in HEADERS + ENTRANCES:
        body = path.read_text(encoding="utf-8")
        assert GUIDE_URL not in body, f"{path.name}: ガイドの URL を直に書いている"


def test_the_learner_goes_straight_to_the_student_page() -> None:
    """**読者に合わせた頁へ送る。** 索引に落とすと、学生は TA 向け・教員向けと
    並んだ一覧から自分の頁を選ぶことになる。教員コンソールは TA と教員の両方が
    使うので、そちらは索引でよい。
    """
    assert guide_url("student") == f"{GUIDE_URL}student/"
    assert guide_url() == GUIDE_URL
