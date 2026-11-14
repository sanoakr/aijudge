"""課題を直す操作は、課題の置き場所・日程・題名を変えない（#699）。

固定したいのは 3 つ。

改訂の採用   AI の改訂を採用しても、課題は位置と日程を保つ（以前はセットの末尾へ移り、
             日程が先頭の課題に揃えられた。監査にも残らなかった）
題名を保つ   画面から保存しても、教員（定義ファイル）が付けた題名は問題文の見出しに
             置き換わらない
見出しに従う 題名が見出しから取った値のままなら、直した見出しに従う（これまでどおり）
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_manage import World, _revision, _task_with_tests
from test_manage import world as world  # フィクスチャを借りる

from aijudge_authoring import TaskSpec
from aijudge_core import Role, Task
from aijudge_core.ids import TaskId
from aijudge_course_admin.authoring import save_task

EXTENDED_DUE = datetime(2026, 12, 24, 9, 0, tzinfo=UTC)
SET_DUE = datetime(2026, 12, 17, 9, 0, tzinfo=UTC)


def _author(world: World):
    from test_manage import _user_id

    return _user_id(world, "teacher")


def _unit_of_three(world: World) -> dict[str, str]:
    """ex01 に p1〜p3 を置き、p2 だけ締切を延ばす。"""
    ids = {"p1": _task_with_tests(world)}
    for name in ("p2", "p3"):
        saved = save_task(
            world.database,
            course_id=world.course.id,
            spec=TaskSpec(
                key=f"ex01/{name}",
                unit="ex01",
                statement=f"## [必須] {name} ##\n\n合計を出力する。",
                position=int(name[1]),
            ),
            subject_profile="cs_lang_c_intro",
            authored_by=_author(world),
        )
        ids[name] = str(saved.task.id)
    with world.database.unit_of_work() as uow:
        for name, task_id in ids.items():
            task = uow.tasks.get_task(TaskId(task_id))
            due = EXTENDED_DUE if name == "p2" else SET_DUE
            uow.tasks.save_task(task.model_copy(update={"due_at": due, "position": int(name[1])}))
        uow.commit()
    return ids


def _task(world: World, task_id: str) -> Task:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
    assert task is not None
    return task


def test_adopting_an_ai_revision_keeps_the_position_and_the_schedule(
    world: World, monkeypatch
) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    ids = _unit_of_three(world)
    _revision(monkeypatch)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{ids['p2']}/revise-with-ai",
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        (draft,) = [d for d in uow.tasks.list_drafts(world.course.id) if d.task_id == ids["p2"]]
    before = _task(world, ids["p2"])

    # 画面の既定のまま（unit を送らない・「いまのまま」）で採用する。
    response = client.post(
        f"/manage/courses/{world.course.id}/drafts/{draft.id}",
        data={"decision": "approve", "unit": draft.unit},
        follow_redirects=False,
    )

    assert response.status_code == 303
    after = _task(world, ids["p2"])
    assert after.position == before.position == 2
    assert after.due_at == before.due_at == EXTENDED_DUE, "延ばした締切が先頭の課題に揃えられた"


def test_the_revision_page_offers_no_unit_to_move_to(world: World, monkeypatch) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    ids = _unit_of_three(world)
    _revision(monkeypatch)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{ids['p2']}/revise-with-ai",
        follow_redirects=False,
    )

    page = client.get(f"/manage/courses/{world.course.id}/drafts").text

    assert "出題する問題セット" not in page
    assert 'name="unit"' not in page


def test_saving_from_the_screen_keeps_a_title_set_outside_the_statement(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p9",
            title="題名は定義ファイルで付けた",
            unit="ex01",
            statement="見出しの無い本文だけ。",
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_author(world),
    )
    task_id = str(saved.task.id)
    assert _task(world, task_id).title == "題名は定義ファイルで付けた"

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "見出しの無い本文だけ。誤字を直した。"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _task(world, task_id).title == "題名は定義ファイルで付けた"


def test_a_title_taken_from_the_heading_follows_the_corrected_heading(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    assert _task(world, task_id).title == "合計"

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 合計を出力する ##\n\n合計を出力する。"},
        follow_redirects=False,
    )

    assert _task(world, task_id).title == "合計を出力する"
