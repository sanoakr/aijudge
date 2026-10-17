"""問題セットの「落ちたテストケースの入力」の設定（2026-10-18）。

すぐ / 締切後 / 見せない を回ごとに選ぶ。固定したいのは 3 つ。

画面に出る      問題セットの画面に選択肢があり、いまの値が選ばれている
選んで保存      保存すると回の全課題に入る
欄が無ければ    欄の無い送信（古い画面）は値を変えない
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import FailedInputs, Role, Task


def _tasks(world: World) -> list[Task]:
    with world.database.unit_of_work() as uow:
        return list(uow.tasks.list_for_course(world.course.id))


def _save(world: World, unit: str, **fields: str):
    return world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/settings",
        data={"session": "", "file_upload": "1"} | fields,
        follow_redirects=False,
    )


def test_the_choice_is_on_the_unit_page(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert 'name="failed_inputs"' in page
    assert '<option value="immediately" selected' in page


def test_choosing_after_deadline_reaches_every_task(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    response = _save(world, unit, failed_inputs="after_deadline")

    assert response.status_code == 303, response.text
    assert {task.failed_inputs for task in _tasks(world)} == {FailedInputs.AFTER_DEADLINE}


def test_a_form_without_the_field_keeps_the_value(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    _save(world, unit, failed_inputs="never")

    response = _save(world, unit, clear_points="5")

    assert response.status_code == 303, response.text
    assert {task.failed_inputs for task in _tasks(world)} == {FailedInputs.NEVER}


def test_an_unknown_choice_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    assert _save(world, unit, failed_inputs="sometimes").status_code == 400
    assert {task.failed_inputs for task in _tasks(world)} == {FailedInputs.IMMEDIATELY}
