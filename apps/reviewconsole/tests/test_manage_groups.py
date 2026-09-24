"""出題先の名簿の画面と、問題セットへの割り当て（`/manage`）。

固定したいのは 5 つ。

作る・置き換える  名簿を貼って保存すると、追加・削除が画面に出る。
突き返す         知らないアカウントが混じれば何も保存せず、入力を残してどれかを言う。
割り当てる       問題セットの画面で選ぶと全課題の出題先になり、外せば全員に戻る。
消せない         出題先として使われている名簿は画面からも消せない。
教員だけ         TA は名簿の画面を開けず、割り当ても変えられない。
"""

from __future__ import annotations

import re

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _groups(world: World) -> str:
    return f"/manage/courses/{world.course.id}/groups"


def _save(world: World, login: str, name: str, members: str):
    return world.client(login).post(
        _groups(world), data={"name": name, "members": members}, follow_redirects=False
    )


def _learners(world: World, *logins: str) -> None:
    for login in logins:
        world.register(login, Role.LEARNER)


def test_saving_a_roster_says_who_was_added_and_removed(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _learners(world, "s2400001", "s2400002", "s2400003")

    first = _save(world, "teacher", "追試", "s2400001\ns2400002")
    second = _save(world, "teacher", "追試", "s2400002, s2400003")

    assert first.status_code == 200
    assert "作りました" in first.text
    assert "置き換えました" in second.text
    assert '追加: <span class="mono">s2400003</span>' in second.text
    assert '削除: <span class="mono">s2400001</span>' in second.text


def test_an_unknown_account_is_sent_back_with_the_input_kept(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _learners(world, "s2400001")

    response = _save(world, "teacher", "追試", "s2400001\ns2499999")

    assert response.status_code == 400
    assert "保存していません" in response.text
    assert "s2499999" in response.text
    assert "s2400001\ns2499999" in response.text, "入力が消えている"
    with world.database.unit_of_work() as uow:
        assert uow.identity.find_group(world.course.id, "追試") is None


def test_assigning_a_unit_and_clearing_it(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _learners(world, "s2400001")
    task_id = _import_example(world)
    unit = _unit_of(world)
    _save(world, "teacher", "追試", "s2400001")
    client = world.client("teacher")

    assigned = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/audience",
        data={"groups": ["追試"]},
        follow_redirects=False,
    )
    assert assigned.status_code == 303
    with world.database.unit_of_work() as uow:
        group = uow.identity.find_group(world.course.id, "追試")
        assert uow.tasks.get_task(TaskId(task_id)).audience_group_ids == (group.id,)
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert re.search(r'value="追試"\s+checked', page), "保存した出題先が選ばれて見えない"

    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/audience", data={}, follow_redirects=False
    )
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).audience_group_ids == ()


def test_a_roster_in_use_cannot_be_deleted_from_the_page(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _learners(world, "s2400001")
    _import_example(world)
    unit = _unit_of(world)
    _save(world, "teacher", "追試", "s2400001")
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/audience",
        data={"groups": ["追試"]},
        follow_redirects=False,
    )

    assert "出題先として使われているので消せません" in client.get(_groups(world)).text
    refused = client.post(f"{_groups(world)}/delete", data={"name": "追試"}, follow_redirects=False)
    assert refused.status_code == 409
    with world.database.unit_of_work() as uow:
        assert uow.identity.find_group(world.course.id, "追試") is not None


def test_an_assistant_cannot_touch_rosters_or_audiences(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    _learners(world, "s2400001")
    task_id = _import_example(world)
    unit = _unit_of(world)
    ta = world.client("ta")

    assert ta.get(_groups(world)).status_code == 403
    assert _save(world, "ta", "追試", "s2400001").status_code == 403
    assert (
        ta.post(
            f"/manage/courses/{world.course.id}/units/{unit}/audience",
            data={"groups": ["追試"]},
            follow_redirects=False,
        ).status_code
        == 403
    )
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).audience_group_ids == ()
