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

**`campus_only` にもあった**（#333 のあと、2026-09-24 に発見）。学内限定の
問題セットの課題を 1 つ直すと、その課題だけ学内限定が外れ、学外から出せた。
欄を 1 つ足すたびに同じ取りこぼしが起きうるので、`Task` の欄の一覧を
ここに固定する（`test_every_field_of_a_task_is_accounted_for`）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin import ensure_course, save_task
from aijudge_authoring import TaskSpec
from aijudge_core import Task
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


def test_a_schedule_only_change_still_takes_effect(world) -> None:
    """**本文を直さずに日程だけ直しても反映される。**

    `TaskVersion` の中身（本文・観点・テストケース）が変わっていなければ
    版は上げない。だが、それは「日程も含めて何もしない」という意味では
    ない ── 以前は `content` が同じというだけで `save_task` がここで
    そのまま抜け、下の `Task` の組み立て（日程を含む）が一度も走らなかった。

    `course.yaml` で `opens_at` だけ直して流し直しても、課題の観点や
    テストケースを一緒に直していなければ何も反映されなかった（実際に
    起きた。network の ex2、2026-09-24。小テストの時間帯と演習課題の
    開放が重なった）。
    """
    database, course = world
    first = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key="ex1/p1", statement="本文", title="課題", unit="ex1"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=True,
    )
    assert first.task.opens_at is None

    second = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key="ex1/p1", statement="本文", title="課題", unit="ex1", opens_at=OPENS),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=True,
    )

    assert second.task.opens_at == OPENS, "本文を変えていないので opens_at が反映されない"
    assert second.version.version == first.version.version, "本文が同じなのに版が増えている"


def test_revising_a_campus_only_task_keeps_the_restriction(world) -> None:
    """**学内限定も引き継ぐ**（#333）。引き継いでいなかったので、学内限定の
    課題の誤字を直すと、その課題だけ学外から出せるようになっていた。
    """
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, campus_only=True)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.campus_only is True, "直したら学内限定が外れている"


# `save_task` が `Task` を作り直すとき、各欄の値がどこから来るか。
# **欄を足したら、ここに足すまでこのファイルが落ちる** ── 足した人に
# 「作り直しで引き継ぐか」を決めさせるため。決めずに足すと、既定値に戻る
# （`withdrawn`・`campus_only` で実際にそうなった）。
FROM_THE_SPEC = {"id", "course_id", "title", "unit", "session", "position"}
FROM_THE_SPEC_OR_KEPT = {"opens_at", "due_at", "accepted_suffixes"}
KEPT = {
    "submissions_open_at",
    "grading_starts_at",
    "accepts_until",
    "auto_finalize_after_minutes",
    "withdrawn",
    "campus_only",
    "confidential_until_open",
    "audience_group_ids",
    "answer_mode",
    "editor_completion",
}
# 版を保存する側が決める（`save_task` は触らない）。
DECIDED_ELSEWHERE = {"current_version_id"}


def test_every_field_of_a_task_is_accounted_for() -> None:
    known = FROM_THE_SPEC | FROM_THE_SPEC_OR_KEPT | KEPT | DECIDED_ELSEWHERE
    added = set(Task.model_fields) - known
    removed = known - set(Task.model_fields)

    assert not added, (
        f"Task に {sorted(added)} が足された。save_task（aijudge_admin.authoring）で"
        "既存の値を引き継ぐかを決めて、このファイルの集合に足すこと"
    )
    assert not removed, f"Task から {sorted(removed)} が無くなった。集合から外すこと"


def test_revising_a_confidential_task_keeps_it_from_assistants(world) -> None:
    """試験の課題を 1 つ直しても、公開前の TA に見えるようにならない。"""
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, confidential_until_open=True)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.confidential_until_open is True, "直したら秘匿が外れている"


def test_revising_a_task_keeps_its_audience(world) -> None:
    """追試の課題を 1 つ直しても、受講者全員に出直さない。"""
    from aijudge_core.ids import CourseGroupId

    database, course = world
    saved = _save(database, course, "本文")
    retake = CourseGroupId("grp_" + "3" * 32)
    _schedule_the_unit(database, saved.task.id, audience_group_ids=(retake,))

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.audience_group_ids == (retake,), "直したら出題先が外れている"


def test_reapplying_unchanged_content_keeps_campus_only(world) -> None:
    """**本文が同じ当て直しでも学内限定が残る。**

    本文が同じなら以前は `save_task` が早く抜けていたので、この経路では
    `campus_only` は消えなかった。早期リターンをやめた（#369）ことで、同じ
    内容の `course apply --revise` も `Task` を作り直すようになった ──
    引き継ぎが無ければ、当て直すたびに学内限定が外れる。
    """
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, campus_only=True)

    _save(database, course, "本文")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.campus_only is True, "同じ内容を当て直したら学内限定が外れている"


def test_revising_an_editor_task_keeps_its_answer_mode(world) -> None:
    """エディタで解く課題を 1 つ直しても、ファイル提出に戻らない（ADR 0026）。"""
    from aijudge_core import AnswerMode

    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, answer_mode=AnswerMode.EDITOR)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.answer_mode is AnswerMode.EDITOR, "直したら答え方が戻っている"


def test_revising_a_task_keeps_its_completion_setting(world) -> None:
    """演習で補完を入れた課題を 1 つ直しても、補完が切に戻らない。"""
    database, course = world
    saved = _save(database, course, "本文")
    _schedule_the_unit(database, saved.task.id, editor_completion=True)

    _save(database, course, "本文（誤字を直した）")

    with database.unit_of_work() as uow:
        after = uow.tasks.get_task(saved.task.id)
    assert after.editor_completion is True, "直したら補完の設定が戻っている"
