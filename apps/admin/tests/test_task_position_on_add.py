"""位置を指定せずに足した課題は、問題セットの末尾に置かれる（#484）。

以前は位置が空のまま保存され、同じ問題セットの中で並びが決まらなかった。
「上へ」「下へ」（`move_task`）も、どれを末尾と見るかが実行ごとに変わった
（段階 0-3a の #480 で、位置の無い 2 問のテストが結果を変えた）。

画面・API・`course apply` は `save_task` を、ディレクトリの取り込みは
`import_tasks` を通るので、その 2 つで確かめる。規則そのものは
`aijudge_core.position_for`。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aijudge_admin import ensure_course, import_tasks, save_task
from aijudge_authoring import TaskSpec
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    yield database, course
    database.dispose()


def _save(
    database,
    course,
    key: str,
    *,
    unit: str | None,
    position: int | None = None,
    statement: str = "問題文",
):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key=key, statement=statement, title=key, unit=unit, position=position),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=True,
    )


def _position(database, task_id) -> int | None:
    with database.unit_of_work() as uow:
        return uow.tasks.get_task(task_id).position


def test_tasks_added_without_a_position_line_up_at_the_end(world) -> None:
    database, course = world
    first = _save(database, course, "ex1/a", unit="ex1")
    second = _save(database, course, "ex1/b", unit="ex1")
    placed = _save(database, course, "ex1/c", unit="ex1", position=5)
    last = _save(database, course, "ex1/d", unit="ex1")
    other_unit = _save(database, course, "ex2/a", unit="ex2")

    assert _position(database, first.task.id) == 1
    assert _position(database, second.task.id) == 2
    assert _position(database, placed.task.id) == 5
    assert _position(database, last.task.id) == 6
    assert _position(database, other_unit.task.id) == 1


def test_saving_again_without_a_position_keeps_the_place(world) -> None:
    """問題文を直すたびに末尾へ移ったり、空に戻ったりしない。"""
    database, course = world
    first = _save(database, course, "ex1/a", unit="ex1")
    _save(database, course, "ex1/b", unit="ex1")

    _save(database, course, "ex1/a", unit="ex1", statement="直した問題文")

    assert _position(database, first.task.id) == 1


def test_an_imported_problem_whose_name_has_no_number_goes_last(world, tmp_path: Path) -> None:
    """`pN` でない名前からは位置が取れない。末尾に置く。"""
    database, course = world
    for name in ("p1", "extra"):
        shutil.copytree(EXAMPLE_TASK, tmp_path / "src" / "ex1" / name)
    (tmp_path / "src" / "ex1" / "extra" / "desc.md").write_text(
        "## 追加の問題\n\n別の問題文\n", encoding="utf-8"
    )

    import_tasks(
        database, course_id=course.id, directory=tmp_path / "src" / "ex1", profiles_dir=PROFILES
    )

    with database.unit_of_work() as uow:
        by_title = {task.title: task.position for task in uow.tasks.list_for_course(course.id)}
    assert sorted(by_title.values()) == [1, 2]
    assert by_title["追加の問題"] == 2
