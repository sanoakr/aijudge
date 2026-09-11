"""左の常設の帯（#189・ADR 0017）。

固定するのは 3 つ。

配られること   34 経路に渡し忘れが無いこと。**帯は `base.html` にあるので、
               1 経路でも来ないとそこだけ骨格の違う画面になる。**
中身           人待ちの件数だけを載せ、押せないものを出さないこと。
落ちないこと   集計が壊れても採点の画面は出ること（P2）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_manage import World  # 同じ世界を使う（作り直すと二重管理になる）

from aijudge_core import Role

REPO_ROOT = Path(__file__).resolve().parents[3]
APP = REPO_ROOT / "apps/reviewconsole/src/aijudge_reviewconsole"


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def _rail(html: str) -> str:
    """帯の部分だけ。"""
    start = html.find('<aside class="rail"')
    assert start != -1, "帯が出ていない"
    return html[start : html.index("</aside>", start)]


# --------------------------------------------------------------------------
# 配られること
# --------------------------------------------------------------------------


def test_every_screen_route_is_reachable_from_a_rail_or_has_no_course() -> None:
    """**経路表と突き合わせる**（ADR 0017 §2）。

    帯はコースを 2 段で解決する ── パスの `course_id` か、ハンドラが
    `request.state` に置いた値。`/review/…` は前者を持たないので後者に
    頼っており、それは `_load()` が 1 か所で置いている。

    ここが守るのは、**その約束を人が覚えていなくてよい**ということ。
    `_load` を通らない `/review/…` の経路が足されたら、数字が静かに
    消えるのではなく、ここで落ちる。
    """
    source = (APP / "app.py").read_text(encoding="utf-8")
    review_routes = re.findall(r'@app\.get\(\s*"(/review/\{submission_id\}[^"]*)"', source)
    assert review_routes, "経路の走査に失敗している"

    # `_load` がコースを教える唯一の場所であること。
    assert source.count("setattr(request.state, RAIL_COURSE_ID") == 1, (
        "帯にコースを教える場所が 1 か所でなくなっている。散らばると、足した経路が忘れられる"
    )
    # すべての `/review/…` の画面が `_load` を通ること。
    # **切り出しはデコレータに固定する** ── 経路の文字列は
    # `RedirectResponse` の中にも出るので、最初の一致だと別物を読む。
    for route in review_routes:
        start = (
            source.index(f'@app.get(\n        "{route}"')
            if f'@app.get(\n        "{route}"' in source
            else source.index(f'@app.get("{route}"')
        )
        handler = source[start:][:1600]
        assert "_load(console, me, SubmissionId(submission_id), request)" in handler, (
            f"{route} が _load を通らない。帯にコースが伝わらず、数字が消える"
        )


def test_a_page_under_a_course_carries_that_courses_rail(world: World) -> None:
    """コースの下の画面には、そのコースの帯が出る。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    for path in (
        f"/courses/{world.course.id}",
        f"/courses/{world.course.id}/submissions",
        f"/manage/courses/{world.course.id}/kc",
    ):
        rail = _rail(client.get(path).text)
        assert world.course.title in rail, f"{path} の帯にコース名が無い"
        assert f"/courses/{world.course.id}/queue" in rail


def test_a_page_without_a_course_carries_the_tenant_rail(world: World) -> None:
    """テナント単位の画面には**別の帯**を出す（ADR 0017 §1）。

    空のコース帯を出すのは、情報が無いことを情報のように見せる。
    """
    world.register("boss", Role.ADMIN)
    rail = _rail(world.client("boss").get("/manage/users").text)
    assert "/manage/subjects" in rail, "テナントの行き先が無い"
    assert f"/courses/{world.course.id}/queue" not in rail, "コースの帯が出ている"


# --------------------------------------------------------------------------
# 中身
# --------------------------------------------------------------------------


def test_the_rail_marks_where_you_are(world: World) -> None:
    """現在地を示す。**`aria-current` で示す** ── 見た目の class を別に
    持つと、支援技術に伝わる状態と画面の状態がずれる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    rail = _rail(world.client("teacher").get(f"/courses/{world.course.id}/submissions").text)
    marked = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', rail)
    assert marked == [f"/courses/{world.course.id}/submissions"], marked


def test_the_rail_does_not_offer_an_assistant_what_they_cannot_open(world: World) -> None:
    """TA に押すと 403 になる行き先を出さない（#102 と同じ扱い）。

    **見えるのに押せないものを並べない。** 問題セットは読めるので出す ──
    採点している課題を読めないと、学習者の質問にも自分の付けた点にも
    答えられない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    rail = _rail(world.client("ta").get(f"/courses/{world.course.id}").text)

    assert f"/manage/courses/{world.course.id}" in rail, "問題セットは TA にも出す"
    for gone in ("/enrolments", "/kc", "/drafts", "/basics"):
        assert gone not in rail, f"TA の帯に {gone} が出ている"


def test_the_rail_counts_only_what_waits_on_a_person(world: World) -> None:
    """**参考の数え上げは載せない**（ADR 0017 §4）。

    提出の総数・受講者数・知識要素の数は「どこに用があるか」を教えないので、
    全ページがクエリを払う理由にならない。行き先は数字が無くても行き先である。
    """
    world.register("teacher", Role.INSTRUCTOR)
    rail = _rail(world.client("teacher").get(f"/courses/{world.course.id}").text)

    def count_of(href: str) -> str | None:
        m = re.search(rf'href="{re.escape(href)}"(.*?)</a>', rail, re.S)
        assert m, f"{href} が帯に無い"
        got = re.search(r'<span class="c[^"]*">([^<]*)</span>', m.group(1))
        return (got.group(1).strip() or None) if got else None

    assert count_of(f"/courses/{world.course.id}/queue") == "0"
    assert count_of(f"/courses/{world.course.id}/submissions") is None, (
        "提出の総数が載っている。どこに用があるかを教えない数字である"
    )


# --------------------------------------------------------------------------
# 落ちないこと
# --------------------------------------------------------------------------


def test_a_broken_count_drops_the_numbers_not_the_page(world: World, monkeypatch) -> None:
    """集計が壊れても採点の画面は出る（ADR 0017 §3・設計原則 P2）。

    帯は成績を読む・付ける機能の前提ではない。ここで例外を上げると、
    数え上げの不調がレビューの画面ごと落とす。
    """
    from aijudge_reviewconsole import rail_context

    def explode(*_args, **_kwargs):
        raise RuntimeError("counting is broken")

    monkeypatch.setattr(rail_context, "_build", explode)
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").get(f"/courses/{world.course.id}")
    assert response.status_code == 200, "帯のせいで画面が落ちている"
    assert '<aside class="rail"' not in response.text


def test_the_rail_does_not_let_a_learner_see_a_course_they_do_not_grade(world: World) -> None:
    """**認可は数え上げの副産物にしない。**

    コースの入口の認可は、以前は異議の件数を数える関数（`_queue_rows`）に
    載っていた。行き先が帯へ移って数え上げが要らなくなったとき、外した
    拍子に認可も一緒に消えた ── テストが捕まえたが、**「数えるついでに
    確かめる」形が残っている限り同じことが起きる。**

    帯そのものも同じ危険を持つ。context processor は全ページで走り、パスの
    `course_id` をそのまま信じる ── そこに他人のコースを入れられたら、
    帯にコース名と件数が出てしまう。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("s2400001", Role.LEARNER)

    response = world.client("s2400001").get(f"/courses/{world.course.id}")
    # **存在しないふりをする** ── 担当していないコースについては、あることも
    # 知らせない（403 は「ある」と言っている）。
    assert response.status_code == 404
    assert world.course.title not in response.text, "帯にコース名が漏れている"


def test_the_demo_course_says_so_to_the_instructor_too(world: World, monkeypatch) -> None:
    """**教員にも出す**（#194）。

    成績を探しに来た人が「ここには無い」と分かる必要がある。文言は学習者と
    同じものを使う ── 違うことを言われると、どちらが本当か確かめることに
    なる。事実は 1 つで、「ここでの提出は残らない」である。
    """
    monkeypatch.setenv("AIJUDGE_DEMO_COURSE", str(world.course.id))
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    body = client.get(f"/courses/{world.course.id}").text
    assert "これはお試しのコースです" in body
    assert "成績にも学習履歴にも残りません" in body
    # 一覧にも印が出る。
    assert "お試し" in client.get("/").text


def test_a_real_course_shows_no_demo_banner(world: World, monkeypatch) -> None:
    """裏返し。**出っぱなしでは意味が無い。**"""
    monkeypatch.delenv("AIJUDGE_DEMO_COURSE", raising=False)
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert "これはお試しのコースです" not in body
