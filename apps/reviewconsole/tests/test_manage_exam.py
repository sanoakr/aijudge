"""問題セットを「試験（監督のある回）」として扱う切り替え（ADR 0032）。

固定したいのは 4 つ。

切り替え      教員がセット単位で切り替え、中の全課題に入る。監査ログに残る。
TA は不可     TA は切り替えられない（読むだけ・#102）。
まとめて保存  設定の画面の 1 つのフォームからも入る。
一覧の印      問題セットの一覧で、試験のセットに「試験」の印が出る。
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _exam(world: World, login: str, unit: str, *, on: bool = True):
    return world.client(login).post(
        f"/manage/courses/{world.course.id}/units/{unit}/exam",
        data={"exam": "1"} if on else {},
        follow_redirects=False,
    )


def _is_exam(world: World, task_id: str) -> bool:
    with world.database.unit_of_work() as uow:
        return uow.tasks.get_task(TaskId(task_id)).exam


def test_an_instructor_marks_a_whole_unit_and_it_is_audited(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    assert _exam(world, "teacher", unit).status_code == 303

    assert _is_exam(world, task_id) is True
    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    changed = [
        e.detail.get("changed", {})
        for e in events
        if e.action.value == "task.updated" and e.detail.get("field") == "exam"
    ]
    assert changed == [{"exam": {"before": False, "after": True}}]

    # 外すのも同じ経路。
    _exam(world, "teacher", unit, on=False)
    assert _is_exam(world, task_id) is False


def test_an_assistant_cannot_switch_it(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)

    assert _exam(world, "ta", _unit_of(world)).status_code == 403
    assert _is_exam(world, task_id) is False


def test_the_settings_form_saves_it_with_the_rest(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    settings = f"/manage/courses/{world.course.id}/units/{unit}/settings"

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'name="exam"' in page

    response = world.client("teacher").post(
        settings,
        data={"session": "", "file_upload": "1", "exam": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _is_exam(world, task_id) is True

    # チェックを外して送れば外れる。
    world.client("teacher").post(
        settings, data={"session": "", "file_upload": "1"}, follow_redirects=False
    )
    assert _is_exam(world, task_id) is False


def test_the_unit_list_marks_an_exam(world: World) -> None:
    """**付け忘れに気づけるように**、問題セットの一覧に印を出す。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    menu = f"/courses/{world.course.id}"

    assert '<span class="pill">試験</span>' not in world.client("teacher").get(menu).text
    _exam(world, "teacher", unit)
    assert '<span class="pill">試験</span>' in world.client("teacher").get(menu).text
