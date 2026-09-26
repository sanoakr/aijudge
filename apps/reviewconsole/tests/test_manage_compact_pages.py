"""設定の画面を詰めて、一覧で読めるようにする（2026-09-26）。

固定したいのは 3 つ。

2 段の一覧       問題セットの行は「どのセットか」と「問数・日程」の 2 段。
                 1 行に 7 列を並べていたので、日付が入ると幅が足りずに折り返した。
同じ時刻は 1 度  開始が公開と同時なら書かない。日程が無ければ 1 語で言う。
控えめな取り消し  受講の取り消しは畳んだ小さな文字から開き、押すときに確かめる。
                 学期中にまず使わない操作が、各行に朱のボタンで並んでいた。
"""

from __future__ import annotations

import re

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _unit_row(page: str, unit: str) -> str:
    match = re.search(rf'<a class="row[^"]*"\s+href="[^"]*/units/{unit}">(.*?)</a>', page, re.S)
    assert match, f"{unit} の行が一覧に無い"
    return match.group(1)


def test_a_unit_is_listed_on_two_lines(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    row = _unit_row(world.client("teacher").get(f"/courses/{world.course.id}").text, unit)

    assert 'class="s-main"' in row
    assert 'class="s-meta desc"' in row
    # 日程が無ければ「未設定」を 3 度並べず、1 語で言う。
    assert "日程未設定" in row
    assert "公開" not in row


def test_the_start_is_omitted_when_it_is_the_opening(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"opens_at": "2026-10-01T10:40", "due_at": "2026-10-15T23:59"},
        follow_redirects=False,
    )

    row = _unit_row(client.get(f"/courses/{world.course.id}").text, unit)

    assert "公開 10-01 10:40" in row
    assert "締切 10-15 23:59" in row
    assert "開始" not in row
    assert "日程未設定" not in row


def test_removing_an_enrolment_is_folded_and_confirmed(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    folded = re.search(r'<details class="quiet-danger">(.*?)</details>', page, re.S)
    assert folded, "取り消しが畳まれていない"
    assert f"/enrolments/{learner.user_id}/remove" in folded.group(1)
    assert "confirm(" in folded.group(1)
    # 畳んだ外側に、取り消しのボタンが出ていない。
    outside = page.replace(folded.group(0), "")
    assert f"/enrolments/{learner.user_id}/remove" not in outside
