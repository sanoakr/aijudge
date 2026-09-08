"""束を入れたら何が起きるかを、入れる前に数える（#161）。

**判定は `save_task` と同じ規則でなければならない。** ここが別の規則だと、
「変化なし」と出したものが保存で版を上げる ── 教員は画面の数字を信じられなく
なる。だから同じ束で「見込み → 保存 → もう一度見込み」を回して確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_bundles import MINIMAL, zipped

from aijudge_admin import ensure_course, save_task
from aijudge_admin.bundle_plan import PlannedChange, plan_bundle
from aijudge_admin.bundles import read_bundle
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習",
        term="2026-前期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    yield database, course
    database.dispose()


def _specs(bundle: bytes):
    return tuple(task.spec for task in read_bundle(bundle))


def _plan(database, course, specs):
    return plan_bundle(
        database,
        course_id=course.id,
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        specs=specs,
    )


def test_everything_is_new_the_first_time(world) -> None:
    database, course = world
    specs = _specs(zipped({"p1/task.yaml": MINIMAL, "p2/task.yaml": MINIMAL}))

    planned = _plan(database, course, specs)

    assert [item.change for item in planned] == [PlannedChange.NEW, PlannedChange.NEW]
    assert [item.existing_version for item in planned] == [None, None]


def test_the_plan_writes_nothing(world) -> None:
    """**見込みは見込みである。** 数えただけで課題が増えたら意味が無い。"""
    database, course = world
    specs = _specs(zipped({"p1/task.yaml": MINIMAL}))

    _plan(database, course, specs)

    with database.unit_of_work() as uow:
        assert uow.tasks.list_for_course(course.id) == ()


def test_the_same_bundle_twice_is_unchanged(world) -> None:
    """入れ直しても何も起きないことを、保存の前に言える。"""
    database, course = world
    specs = _specs(zipped({"p1/task.yaml": MINIMAL}))
    for spec in specs:
        save_task(
            database,
            course_id=course.id,
            spec=spec,
            subject_profile=course.subject_profile,
            authored_by=TEACHER,
        )

    planned = _plan(database, course, specs)

    assert [item.change for item in planned] == [PlannedChange.UNCHANGED]
    assert planned[0].existing_version == 1


def test_changed_content_shows_as_a_new_version(world) -> None:
    """過去の採点基準は書き換えない（P8）ので、直した課題は版が上がる。"""
    database, course = world
    save_task(
        database,
        course_id=course.id,
        spec=_specs(zipped({"p1/task.yaml": MINIMAL}))[0],
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    changed = _specs(zipped({"p1/task.yaml": MINIMAL.replace("本文", "直した本文")}))
    planned = _plan(database, course, changed)

    assert planned[0].change is PlannedChange.REVISED
    assert planned[0].existing_version == 1


def test_the_plan_agrees_with_what_saving_actually_does(world) -> None:
    """**規則を 2 か所に書かない。** 見込みと保存が食い違わないことを回して見る。"""
    database, course = world
    specs = _specs(zipped({"p1/task.yaml": MINIMAL, "p2/task.yaml": MINIMAL}))

    for item in _plan(database, course, specs):
        assert item.change is PlannedChange.NEW
    for spec in specs:
        saved = save_task(
            database,
            course_id=course.id,
            spec=spec,
            subject_profile=course.subject_profile,
            authored_by=TEACHER,
        )
        assert saved.created

    # 2 周目: 見込みが「変化なし」なら、保存も何も作らない。
    for item in _plan(database, course, specs):
        assert item.change is PlannedChange.UNCHANGED
    for spec in specs:
        saved = save_task(
            database,
            course_id=course.id,
            spec=spec,
            subject_profile=course.subject_profile,
            authored_by=TEACHER,
        )
        assert not saved.created


def test_the_title_matches_what_saving_will_show(world) -> None:
    database, course = world
    specs = _specs(zipped({"p1/task.yaml": MINIMAL}))

    planned = _plan(database, course, specs)
    saved = save_task(
        database,
        course_id=course.id,
        spec=specs[0],
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    assert planned[0].title == saved.task.title
