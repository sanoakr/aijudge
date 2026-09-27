"""課題ごとの実行時間の上限を、課題の画面から変える（#491）。"""

from __future__ import annotations

from test_manage import World, _task_with_tests
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _url(world: World, task_id: str, tail: str) -> str:
    return f"/manage/courses/{world.course.id}/tasks/{task_id}/{tail}"


def test_the_task_page_offers_the_limit_with_its_default(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)

    page = world.client("teacher").get(_url(world, task_id, "edit")).text

    assert 'name="case_timeout_seconds"' in page
    # 空欄のとき何秒になるかを出す（例題の科目は 2 秒）。
    assert 'placeholder="2"' in page


def test_saving_the_limit_does_not_raise_the_version(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    response = client.post(
        _url(world, task_id, "case-timeout"),
        data={"case_timeout_seconds": "30"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("saved=task_case_timeout#saved")

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        version = uow.tasks.latest_version(TaskId(task_id))
        audit = uow.audit.list_for_target("task", task_id)
    assert task.case_timeout_seconds == 30.0
    assert version.version == 1, "上限を変えただけで版が上がっている"
    assert any(row.summary == "課題の実行時間の上限を変えた" for row in audit)

    page = client.get(_url(world, task_id, "edit?saved=task_case_timeout")).text
    assert 'value="30"' in page
    assert "実行時間の上限を保存しました" in page


def test_an_empty_field_returns_to_the_default(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")
    client.post(_url(world, task_id, "case-timeout"), data={"case_timeout_seconds": "30"})

    client.post(_url(world, task_id, "case-timeout"), data={"case_timeout_seconds": ""})

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).case_timeout_seconds is None


def test_a_limit_past_the_ceiling_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    for bad in ("61", "0", "-1", "abc"):
        response = client.post(
            _url(world, task_id, "case-timeout"),
            data={"case_timeout_seconds": bad},
            follow_redirects=False,
        )
        assert response.status_code == 400, bad

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).case_timeout_seconds is None


def test_an_assistant_cannot_change_the_limit(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _task_with_tests(world)

    response = world.client("ta").post(
        _url(world, task_id, "case-timeout"),
        data={"case_timeout_seconds": "30"},
        follow_redirects=False,
    )

    assert response.status_code in (403, 404)
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).case_timeout_seconds is None
