"""問題セットを「公開まで教員だけに見せる」に切り替える画面（試験）。

固定したいのは 4 つ。

切り替え     教員がセット単位で切り替え、中の全課題に入る。監査ログに残る。
TA は不可    TA は切り替えられない（読むだけ・#102）。
読む画面     TA の読むだけの課題画面も、公開前の試験なら 404。
効いていない  公開の時刻が無いなら、切り替えても効かないとはっきり書く。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _confidential(world: World, login: str, unit: str, *, on: bool = True):
    return world.client(login).post(
        f"/manage/courses/{world.course.id}/units/{unit}/confidential",
        data={"confidential": "1"} if on else {},
        follow_redirects=False,
    )


def _opens(world: World, task_id: str, when: datetime | None) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        uow.tasks.save_task(task.model_copy(update={"opens_at": when}))
        uow.commit()


def test_an_instructor_marks_a_whole_unit_and_it_is_audited(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    assert _confidential(world, "teacher", unit).status_code == 303

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).confidential_until_open is True
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    changed = [
        e.detail.get("changed", {})
        for e in events
        if e.action.value == "task.updated" and e.detail.get("field") == "confidential"
    ]
    assert changed == [{"confidential_until_open": {"before": False, "after": True}}]

    # 外すのも同じ経路。
    _confidential(world, "teacher", unit, on=False)
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).confidential_until_open is False


def test_an_assistant_cannot_switch_it(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)

    response = _confidential(world, "ta", _unit_of(world))

    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).confidential_until_open is False


def test_the_read_only_task_page_is_hidden_from_an_assistant_before_opening(
    world: World,
) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    _confidential(world, "teacher", _unit_of(world))
    edit = f"/manage/courses/{world.course.id}/tasks/{task_id}/edit"

    _opens(world, task_id, datetime.now(UTC) + timedelta(days=1))
    assert world.client("ta").get(edit).status_code == 404
    assert world.client("teacher").get(edit).status_code == 200

    # 公開されたら TA にも読める（質問対応と採点のため）。
    _opens(world, task_id, datetime.now(UTC) - timedelta(minutes=1))
    assert world.client("ta").get(edit).status_code == 200


def test_the_unit_page_does_not_list_it_for_an_assistant(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    unit = _unit_of(world)
    _confidential(world, "teacher", unit)
    _opens(world, task_id, datetime.now(UTC) + timedelta(days=1))
    page = f"/manage/courses/{world.course.id}/units/{unit}"

    assert task_id not in world.client("ta").get(page).text
    assert task_id in world.client("teacher").get(page).text


def test_the_unit_page_warns_when_there_is_no_opening_time(world: World) -> None:
    """**切り替えただけで守られていると読まれるのが、いちばん高くつく誤解。**

    公開の時刻が無い課題は、はじめから公開済みとして扱う（`Task.before_open_at`）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    _confidential(world, "teacher", unit)
    page = f"/manage/courses/{world.course.id}/units/{unit}"

    _opens(world, task_id, None)
    assert "この設定は効いていません" in world.client("teacher").get(page).text

    _opens(world, task_id, datetime.now(UTC) + timedelta(days=1))
    assert "この設定は効いていません" not in world.client("teacher").get(page).text
