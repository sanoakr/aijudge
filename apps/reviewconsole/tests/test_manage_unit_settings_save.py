"""問題セットの設定をまとめて保存する（2026-09-26）。

以前は節ごとに保存ボタンがあり（9 つ）、1 つを押すと頁が読み直されて、他の節で
書きかけていた値が黙って消えた。固定したいのは 4 つ。

ボタンは 1 つ        設定は 1 つのフォームで、節ごとの保存の送り先は画面に無い。
変えたものだけ書く    ばらついている値を、触っていない節の保存で揃えない。
1 つ断られたら全部    日程が断られたら、同時に送った回番号も書かれない。
検査は同じ関数        答え方の組み合わせは、個別の経路と同じ理由で断られる。
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role, Task
from aijudge_core.ids import TaskId


def _tasks(world: World) -> list[Task]:
    with world.database.unit_of_work() as uow:
        return list(uow.tasks.list_for_course(world.course.id))


def _task(world: World, task_id: str) -> Task:
    with world.database.unit_of_work() as uow:
        return uow.tasks.get_task(TaskId(task_id))


def _form(**overrides: str) -> dict[str, str]:
    """画面を開いてそのまま送ったときの値（例題の問題セットの既定）。"""
    return {"session": "", "file_upload": "1"} | overrides


def _settings(world: World, unit: str) -> str:
    return f"/manage/courses/{world.course.id}/units/{unit}/settings"


def test_the_page_has_one_form_and_one_save_button(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert page.count(f'/units/{unit}/settings"') == 1
    assert "設定を保存" in page
    for gone in (
        "/number",
        "/schedule",
        "/campus-only",
        "/answer-mode",
        "/clear-points",
        "/completion",
        "/confidential",
        "/auto-finalize",
        "/screen-capture",
    ):
        assert f'/units/{unit}{gone}"' not in page, f"{gone} の保存ボタンが残っている"


def test_everything_sent_together_is_saved_together(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        _settings(world, unit),
        data=_form(
            session="3",
            opens_at="2026-10-01T10:40",
            due_at="2026-10-15T23:59",
            campus_only="1",
            clear_points="5",
            after_minutes="30",
            screen_capture="1",
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "saved=settings" in response.headers["location"]
    task = _task(world, task_id)
    assert task.session == 3
    assert task.due_at is not None and task.opens_at is not None
    assert task.campus_only is True
    assert task.clear_points == 5
    assert task.auto_finalize_after_minutes == 30
    assert task.screen_capture is True


def test_a_refusal_writes_nothing(world: World) -> None:
    """**1 つ断られたら全部。** 締切が公開より前なら、回番号も書かれない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        _settings(world, unit),
        data=_form(session="7", opens_at="2026-10-15T10:00", due_at="2026-10-01T10:00"),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "締切" in response.json()["detail"]
    assert _task(world, task_id).session is None


def test_an_untouched_mixed_value_is_left_mixed(world: World) -> None:
    """**変えたものだけ書く。** 日程を直しに来た保存で、ばらついた学内限定を揃えない。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={"statement": "## 2 問目 ##\n\n本文", "key_suffix": "second", "unit": unit},
        follow_redirects=False,
    )
    first, second = sorted(_tasks(world), key=lambda t: str(t.id))
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(Task.model_validate(first.model_dump() | {"campus_only": True}))
        uow.commit()
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "値がばらついています" in page

    # 画面の代表値のまま（学内限定は外れて見える）、日程だけ変えて送る。
    client.post(
        _settings(world, unit),
        data=_form(due_at="2026-10-15T23:59"),
        follow_redirects=False,
    )

    after = {t.id: t for t in _tasks(world)}
    assert {after[first.id].campus_only, after[second.id].campus_only} == {True, False}
    assert all(t.due_at is not None for t in after.values())


def test_nothing_changed_writes_nothing(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")  # ログインも記録に残るので、先に済ませてから数える
    with world.database.unit_of_work() as uow:
        before = len(uow.audit.list_recent(world.course.tenant_id, limit=100))

    response = client.post(_settings(world, unit), data=_form(), follow_redirects=False)

    assert "saved=unchanged" in response.headers["location"]
    with world.database.unit_of_work() as uow:
        assert len(uow.audit.list_recent(world.course.tenant_id, limit=100)) == before


def test_the_audit_names_only_what_changed(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    world.client("teacher").post(
        _settings(world, unit), data=_form(session="2"), follow_redirects=False
    )

    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    event = next(e for e in events if e.detail.get("field") == "settings")
    assert set(event.detail["changed"]) == {"session"}


def test_no_way_to_submit_is_refused_as_in_the_single_route(world: World) -> None:
    """**検査は同じ関数。** ファイルもエディタも外すと、誰も提出できない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        _settings(world, unit), data={"session": "4"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert _task(world, task_id).session is None


def test_an_assistant_cannot_save(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    _import_example(world)
    unit = _unit_of(world)

    response = world.client("ta").post(
        _settings(world, unit), data=_form(session="9"), follow_redirects=False
    )

    assert response.status_code in (403, 404)
