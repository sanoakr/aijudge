"""左の帯の件数を、ページ全体を取り直さずに取り直す（`/rail-fragment`・2026-10-08）。

件数は `rail_context` が全ページに描くので、開き直さないと変わらなかった。固定したいのは 4 つ。

取り直し先が帯にある    帯に `data-refresh-url` と `data-course-id` が付く（`live.js` が読む）
帯だけを返す           ページの本文は返さない
同じ帯                 最初に描いた帯と、取り直した帯が同じ組み立て（現在地の印も同じ）
認可は帯が確かめる      受講していないコースの帯は、コース名も件数も返さない
"""

from __future__ import annotations

from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role

LIVE = {"X-Aijudge-Live": "1"}


def _aside(html: str) -> str:
    start = html.find('<aside class="rail"')
    assert start != -1, "帯が無い"
    return html[start : html.index("</aside>", start) + len("</aside>")]


def test_the_rail_carries_where_to_refresh_from(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/courses/{world.course.id}/finalize").text

    rail = _aside(page)
    assert 'data-refresh-url="' in rail and "/rail-fragment" in rail
    assert f'data-course-id="{world.course.id}"' in rail


def test_the_fragment_is_the_rail_only_and_matches_the_first_render(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")
    path = f"/courses/{world.course.id}/finalize"
    first = _aside(client.get(path).text)

    response = client.get(
        "/rail-fragment", params={"course_id": str(world.course.id), "path": path}, headers=LIVE
    )

    assert response.status_code == 200
    fragment = response.text
    assert fragment.lstrip().startswith("<aside"), "帯以外のものが混ざっている"
    assert "<html" not in fragment and "<main" not in fragment
    assert fragment.strip() == first.strip(), "取り直した帯が最初の帯と違う"
    assert 'aria-current="page"' in fragment, "現在地の印が付いていない"


def test_a_course_the_user_does_not_teach_leaks_nothing(world: World) -> None:
    """**帯が自分で認可する。** 受講していないコースを指しても、コース名も件数も出ない。"""
    world.register("outsider", None)
    client = world.client("outsider")

    response = client.get(
        "/rail-fragment",
        params={"course_id": str(world.course.id), "path": "/"},
        headers=LIVE,
    )

    assert world.course.title not in response.text, "受講していないコース名が出ている"
    assert str(world.course.id) not in response.text


def test_the_fragment_needs_a_login(world: World) -> None:
    from fastapi.testclient import TestClient

    from aijudge_reviewconsole.app import create_app

    anonymous = TestClient(create_app(world.console))
    assert anonymous.get("/rail-fragment", headers=LIVE).status_code in (401, 303, 307)
