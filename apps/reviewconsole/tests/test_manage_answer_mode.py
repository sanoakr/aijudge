"""問題セットの答え方の切り替え（ADR 0026）。

固定したいのは 3 つ。

セット単位     学内限定と同じく、中の全課題に同じ値が入る。
保存時の検査   テストを走らせない課題は `editor` にできない。画面で押せなく
               するだけでは境界にならない（#146）ので、保存する側が断る。
既定           何もしなければ従来どおりファイル提出（不変条件 I4・I5）。
"""

from __future__ import annotations

import pytest
from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_admin.answer_mode import editor_blockers
from aijudge_core import AnswerMode, Role
from aijudge_core.ids import TaskId


def _pair(world: World):
    from aijudge_admin import list_tasks

    return list_tasks(world.database, world.course.id)[0]


def _task(world: World, task_id: str):
    with world.database.unit_of_work() as uow:
        return uow.tasks.get_task(TaskId(task_id))


def test_a_task_answers_by_upload_unless_told_otherwise(world: World) -> None:
    task_id = _import_example(world)
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_an_instructor_puts_a_whole_unit_in_the_editor(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "editor"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _task(world, task_id).answer_mode is AnswerMode.EDITOR
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "答え方" in page

    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "upload"},
        follow_redirects=False,
    )
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_switching_is_audited(world: World) -> None:
    """学習者の画面が変わる操作なので、誰がいつ変えたかを残す。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "editor"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    assert any(e.detail.get("field") == "answer_mode" for e in events)


def test_the_server_refuses_the_editor_for_a_task_that_runs_no_tests(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**保存する側が断る。** 画面は押せなくしてあるが、それは境界ではない。"""
    import aijudge_reviewconsole.manage as manage

    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    monkeypatch.setattr(
        manage, "editor_blockers", lambda *a, **k: ("tsk_x: テストを走らせない課題です",)
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "editor"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "テストを走らせない" in response.text
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


def test_a_bogus_mode_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "telepathy"},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_an_assistant_cannot_switch(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/units/{unit}/answer-mode",
        data={"answer_mode": "editor"},
        follow_redirects=False,
    )

    assert response.status_code in (403, 404)
    assert _task(world, task_id).answer_mode is AnswerMode.UPLOAD


# -- 検査そのもの ------------------------------------------------------------


def test_the_rule_accepts_a_task_that_takes_code(world: World) -> None:
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".c",)})
    assert editor_blockers([(task, version)], world.course) == ()


def test_the_rule_accepts_a_report_written_as_text(world: World) -> None:
    """オンラインのレポート試験。`.md` だけの課題もエディタにできる。"""
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".md",)})
    assert editor_blockers([(task, version)], world.course) == ()


def test_the_rule_names_a_task_that_takes_nothing_the_editor_writes(world: World) -> None:
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": (".pdf", ".jpg")})

    reasons = editor_blockers([(task, version)], world.course)

    assert len(reasons) == 1
    assert task.title in reasons[0] and ".pdf" in reasons[0]


def test_the_rule_falls_back_to_the_courses_formats(world: World) -> None:
    """課題が形式を指定しなければコースの既定（受付と同じ `allowed_suffixes`）。"""
    task, version = _pair(world_with_example(world))
    task = task.model_copy(update={"accepted_suffixes": ()})
    pdf_only = world.course.model_copy(update={"upload_suffixes": (".pdf",)})

    assert editor_blockers([(task, version)], pdf_only) != ()
    assert editor_blockers([(task, version)], world.course) == ()


def world_with_example(world: World) -> World:
    _import_example(world)
    return world
