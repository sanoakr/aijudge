"""課題を直しても、その課題が持つ日程は動かない。

日程は問題セットで決めるが、値を持つのは課題である
（`aijudge_core.task.Task`）。`TaskSpec` には公開と締切の欄しか無いので、
版を上げるたびに `Task` を作り直す `save_task` は、**欄の無い日程を既定
（空）へ戻していた** ── 問題セットで揃えたあとに課題を 1 つ直すと、その課題
だけ提出開始・受付終了・採点開始・猶予が抜け、問題セットの画面が「日程が
課題ごとにばらついています」と言い続けた。教員は揃えたのに、揃えた操作が
揃えたものを壊していた。

同じ取りこぼしが `withdrawn` にもあり、こちらは結果が重い ── 取り下げた
課題の誤字を直すと、学習者に出直していた。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin import ensure_course, save_task
from aijudge_authoring import TaskSpec
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)

OPENS = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
SUBMITS = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
DUE = datetime(2026, 9, 25, 14, 59, tzinfo=UTC)
GRADES = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
ACCEPTS = datetime(2026, 10, 2, 14, 59, tzinfo=UTC)
GRACE_MINUTES = 4320


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


def _save(database, course, statement: str):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key="ex1/p1", statement=statement, title="課題", unit="ex1"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=True,
    )


def _schedule_the_unit(database, task_id, **extra):
    """問題セットの画面が全課題に当てるのと同じ更新（`_update_unit`）。"""
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        uow.tasks.save_task(
            task.model_copy(
                update={
                    "opens_at": OPENS,
                    "submissions_open_at": SUBMITS,
                    "due_at": DUE,
                    "grading_starts_at": GRADES,
                    "accepts_until": ACCEPTS,
                    "auto_finalize_after_minutes": GRACE_MINUTES,
                    **extra,
                }
            )
        )
        uow.commit()


def test_revising_a_task_keeps_the_whole_schedule(world) -> None:
    """**6 つ全部が残る。** 公開と締切だけ引き継いでいたのが元の欠陥で、
    残り 4 つは版を上げるたびに空へ戻っていた。
    """
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.opens_at == OPENS
    assert after.submissions_open_at == SUBMITS, "提出開始が消えている"
    assert after.due_at == DUE
    assert after.grading_starts_at == GRADES, "採点開始が消えている（試験が壊れる・#67）"
    assert after.accepts_until == ACCEPTS, "受付終了が消えている（#73）"
    assert after.auto_finalize_after_minutes == GRACE_MINUTES, "猶予が消えている"


def test_revising_a_withdrawn_task_does_not_publish_it(world) -> None:
    """**取り下げは削除ではない**（#83）。取り下げた課題の誤字を直したら
    学習者に出直した、が元の姿である。
    """
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, withdrawn=True)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.withdrawn is True, "直したら出題が戻っている"


def test_a_new_task_starts_with_an_empty_schedule(world) -> None:
    """引き継ぐのは**既にある課題のものだけ**。新しい課題に前の課題の日程が
    移ると、問題セットをまたいで締切が伝染する。
    """
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id)

    other = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key="ex1/p2", statement="別の本文", title="別の課題", unit="ex1"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    assert other.task.due_at is None
    assert other.task.accepts_until is None
    assert other.task.withdrawn is False
