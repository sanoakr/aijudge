"""問題セットの「教員の確認後に結果を返す」の設定（ADR 0033）。

画面に出る      問題セットの画面にチェックがある
保存            チェックして保存すると回の全課題に入り、外すと戻る
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role, Task


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

    assert 'name="results_after_review"' in page


def test_checking_it_reaches_every_task_and_unchecking_undoes_it(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    response = _save(world, unit, results_after_review="1")

    assert response.status_code == 303, response.text
    assert all(task.results_after_review for task in _tasks(world))

    response = _save(world, unit)

    assert response.status_code == 303, response.text
    assert not any(task.results_after_review for task in _tasks(world))
