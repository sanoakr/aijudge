"""画面から問題セットに課題を足したとき、セットの値を全部引き継ぐ（2026-11-14・#709）。

セットの画面は「ばらつき」を出すが、保存のときには何も断られない。以前は日程と提出形式の
6 欄だけを先頭の課題から写し、学内限定・公開まで秘匿・試験・出題先・受付終了・採点開始・
確認後に返す設定は既定のまま入った ── 公開前のセットに 1 問足すと、その 1 問だけ TA に見えた。

固定したいのは 3 つ。

引き継ぐ    足した課題は、セットの値（`UNIT_WIDE_FIELDS`）を先頭の課題と同じに持つ
見えない    公開前の試験に足した課題は、TA の画面でも 404（先頭の課題と同じ）
採用でも    下書きを承認してセットへ置く経路（`_place_in_unit`）も同じ値を揃える
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_manage import World, _import_example, _unit_of

from aijudge_core import UNIT_WIDE_FIELDS, AnswerMode, Role, Task
from aijudge_core.ids import CourseGroupId

GROUP = CourseGroupId("grp_" + "b" * 32)
FUTURE = datetime.now(UTC) + timedelta(days=30)


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def _make_the_unit_an_exam(world: World, unit: str) -> None:
    """先頭の課題に、既定とは違う値を付ける（既定のままなら取りこぼしが見えない）。"""
    with world.database.unit_of_work() as uow:
        (head,) = [t for t in uow.tasks.list_for_course(world.course.id) if t.unit == unit]
        uow.tasks.save_task(
            Task.model_validate(
                head.model_dump()
                | {
                    "opens_at": FUTURE,
                    "due_at": FUTURE + timedelta(hours=1),
                    "accepts_until": FUTURE + timedelta(hours=1),
                    "grading_starts_at": FUTURE + timedelta(hours=1),
                    "campus_only": True,
                    "campus_ranges": ("10.0.0.0/24",),
                    "confidential_until_open": True,
                    "exam": True,
                    "audience_group_ids": (GROUP,),
                    "answer_mode": AnswerMode.EDITOR,
                    "results_after_review": True,
                }
            )
        )
        uow.commit()


def _unit_tasks(world: World, unit: str) -> list[Task]:
    with world.database.unit_of_work() as uow:
        return [t for t in uow.tasks.list_for_course(world.course.id) if t.unit == unit]


def _add(world: World, unit: str, suffix: str = "p9"):
    return world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": suffix,
            "unit": unit,
            "statement": f"## [必須] 追加した課題 {suffix} ##\n\n本文",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )


def test_a_task_added_to_an_exam_unit_gets_every_unit_value(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    _make_the_unit_an_exam(world, unit)

    response = _add(world, unit)
    assert response.status_code == 303, response.text

    head, added = sorted(_unit_tasks(world, unit), key=lambda t: t.sort_key)
    assert added.title == "追加した課題 p9"
    for name in UNIT_WIDE_FIELDS:
        assert getattr(added, name) == getattr(head, name), f"{name} が先頭の課題と揃っていない"
    assert added.confidential_until_open and added.exam and added.campus_only
    assert added.audience_group_ids == (GROUP,)


def test_a_new_task_in_an_unopened_exam_is_hidden_from_an_assistant(world: World) -> None:
    """秘匿が付かないと、公開前の試験の問題を TA が読めた。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    _import_example(world)
    unit = _unit_of(world)
    _make_the_unit_an_exam(world, unit)
    _add(world, unit)
    head, added = sorted(_unit_tasks(world, unit), key=lambda t: t.sort_key)

    ta = world.client("ta")
    base = f"/manage/courses/{world.course.id}/tasks"
    assert ta.get(f"{base}/{head.id}/edit").status_code == 404
    assert ta.get(f"{base}/{added.id}/edit").status_code == 404


def test_a_task_added_to_an_empty_unit_keeps_the_defaults(world: World) -> None:
    """揃える相手が居なければ、今までどおり既定のまま入る。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = _add(world, "exam99", suffix="q1")
    assert response.status_code == 303, response.text

    (added,) = _unit_tasks(world, "exam99")
    assert not added.confidential_until_open and not added.exam and not added.campus_only


def test_placing_an_approved_draft_aligns_every_unit_value(world: World) -> None:
    """下書きを承認してセットへ置く経路（`_place_in_unit`）も、同じ一覧を使う。"""
    from aijudge_reviewconsole.manage.drafts import _place_in_unit

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    _make_the_unit_an_exam(world, unit)
    _add(world, "exam99", suffix="q1")
    (loose,) = _unit_tasks(world, "exam99")

    _place_in_unit(world.console, world.course, loose, unit)

    head, placed = sorted(_unit_tasks(world, unit), key=lambda t: t.sort_key)
    assert placed.id == loose.id
    for name in UNIT_WIDE_FIELDS:
        assert getattr(placed, name) == getattr(head, name), f"{name} が先頭の課題と揃っていない"
