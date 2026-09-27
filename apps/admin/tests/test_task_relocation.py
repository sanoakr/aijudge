"""課題の移動・名前の変更・コピー（2026-09-27、network の `test4/echoClient`）。

**キーの頭は問題セット。** 移したら付け替える。課題 ID はキーから導かれるので
付け替え＝別の課題を作ることになり、学生の提出がある課題は移せない（コピーする）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring import DraftKind, TaskDraftRecord, TaskSpec
from aijudge_core import Role, Submission, SubmissionState
from aijudge_core.ids import SubmissionId, TenantId, UserId, derived_id
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.operations import AdminError, ensure_course
from aijudge_course_admin.task_relocation import compose_key, copy_task, key_name, move_task
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
DUE = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/relocate.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    return obj


def _task(database: Database, course, key: str, unit: str, *, due_at=None, revise=False):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key=key,
            statement=f"## [必須] {key} ##\n\n本文{'（直した）' if revise else ''}",
            unit=unit,
            readability_weight=0.3,
            due_at=due_at,
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=TEACHER,
        revise=revise,
    )


def _submit(database: Database, saved, suffix: str, *, as_role: Role) -> None:
    with database.unit_of_work() as uow:
        uow.submissions.save(
            Submission(
                id=SubmissionId("sub_" + suffix * 32),
                task_version_id=saved.version.id,
                learner_id=LEARNER,
                state=SubmissionState.DRAFT,
                attempt=1,
                created_at=datetime.now(UTC),
                submitted_as=as_role,
            )
        )
        uow.commit()


def test_compose_key_and_key_name() -> None:
    assert compose_key("ex02", "p8") == "ex02/p8"
    assert compose_key("", "p8") == "p8"
    assert compose_key("ex02", "ex02/p8") == "ex02/p8"
    assert key_name("test5/echoClient", "test5") == "echoClient"
    # 移したのに付け替わっていない古い課題でも、名前は最後の区切りの後ろ。
    assert key_name("test4/echoClient", "test5") == "echoClient"


def test_moving_rekeys_the_task_and_follows_the_target_schedule(database: Database, course) -> None:
    """**test4 から test5 へ移すと `test5/…` になる。** 元の課題は残らない。"""
    _task(database, course, "test5/echoClient5", "test5", due_at=DUE)
    first = _task(database, course, "test4/echoClient", "test4")
    second = _task(database, course, "test4/echoClient", "test4", revise=True)
    assert second.version.version == 2

    moved = move_task(database, task_id=first.task.id, unit="test5", name="echoClient_comments.py")

    assert moved.key == "test5/echoClient_comments.py"
    assert moved.previous_key == "test4/echoClient"
    assert str(moved.task.id) == derived_id("tsk", str(course.id), moved.key)
    with database.unit_of_work() as uow:
        assert uow.tasks.get_task(first.task.id) is None
        task = uow.tasks.get_task(moved.task.id)
        versions = uow.tasks.list_versions(moved.task.id)
    assert task is not None and task.unit == "test5"
    assert task.due_at == DUE, "日程が移動先に揃っていない"
    assert task.position == 2, "移動先の末尾に入っていない"
    # **全版を運ぶ。** 版番号と中身はそのまま、鍵だけが新しい。
    versions = sorted(versions, key=lambda v: v.version)
    assert [v.version for v in versions] == [1, 2]
    assert {v.source_key for v in versions} == {"test5/echoClient_comments.py"}
    assert "直した" in versions[-1].statement
    assert all(entry.task_version_id == v.id for v in versions for entry in v.q_matrix)


def test_renaming_within_the_same_unit(database: Database, course) -> None:
    saved = _task(database, course, "test5/echoClient", "test5")
    moved = move_task(database, task_id=saved.task.id, unit="test5", name="echoClient_comments.py")
    assert moved.key == "test5/echoClient_comments.py"
    assert moved.task.position == saved.task.position, "同じセットの中で並びが動いた"


def test_a_task_with_learner_submissions_cannot_move(database: Database, course) -> None:
    """**成績が課題版を指している。** 付け替えると何の課題の点なのか辿れなくなる。"""
    saved = _task(database, course, "ex1/p2", "ex1")
    _submit(database, saved, "a", as_role=Role.LEARNER)

    with pytest.raises(AdminError, match="コピー"):
        move_task(database, task_id=saved.task.id, unit="ex2")
    with database.unit_of_work() as uow:
        assert uow.tasks.get_task(saved.task.id) is not None


def test_trial_submissions_do_not_block_a_move_and_are_removed(database: Database, course) -> None:
    """**お試しの提出は妨げない**（2026-09-27 の決定）。付け替えのときに消す。"""
    saved = _task(database, course, "test3/nstring2", "test3")
    _submit(database, saved, "b", as_role=Role.INSTRUCTOR)

    moved = move_task(database, task_id=saved.task.id, unit="test4")

    assert moved.key == "test4/nstring2" and moved.trial_submissions == 1
    with database.unit_of_work() as uow:
        assert uow.submissions.get(SubmissionId("sub_" + "b" * 32)) is None


def test_a_taken_key_is_refused(database: Database, course) -> None:
    _task(database, course, "test5/echoClient", "test5")
    other = _task(database, course, "test4/echoClient", "test4")
    with pytest.raises(AdminError, match="test5/echoClient"):
        move_task(database, task_id=other.task.id, unit="test5")


def test_moving_to_the_same_place_is_refused(database: Database, course) -> None:
    saved = _task(database, course, "test4/x", "test4")
    with pytest.raises(AdminError, match="同じ"):
        move_task(database, task_id=saved.task.id, unit="test4")


def test_a_revision_draft_follows_the_moved_task(database: Database, course) -> None:
    """改訂の下書きは元の課題を指す。付け替えないと、採用で元のキーの課題が蘇る。"""
    saved = _task(database, course, "test4/echoClient", "test4")
    with database.unit_of_work() as uow:
        uow.tasks.save_draft(
            TaskDraftRecord(
                id="dft_" + "3" * 32,
                course_id=course.id,
                kind=DraftKind.REVISION,
                task_id=saved.task.id,
                spec=TaskSpec(key="test4/echoClient", statement="改訂案", unit="test4"),
                unit="test4",
                created_by=TEACHER,
                created_at=datetime.now(UTC),
                subject_profile="cs_lang_c_intro",
            )
        )
        uow.commit()

    moved = move_task(database, task_id=saved.task.id, unit="test5")

    with database.unit_of_work() as uow:
        draft = uow.tasks.get_draft("dft_" + "3" * 32)
    assert draft is not None
    assert draft.task_id == moved.task.id and draft.spec.key == "test5/echoClient"


def test_copying_keeps_the_original_and_its_submissions(database: Database, course) -> None:
    """**提出のある課題はコピーで別の問題セットへ。** 元の課題と成績はそのまま。"""
    saved = _task(database, course, "ex1/p2", "ex1")
    _submit(database, saved, "c", as_role=Role.LEARNER)

    copied = copy_task(database, task_id=saved.task.id, unit="ex3", name="p2_again")

    assert copied.key == "ex3/p2_again"
    with database.unit_of_work() as uow:
        assert uow.tasks.get_task(saved.task.id) is not None
        assert uow.tasks.submission_count(saved.task.id) == 1
        assert uow.tasks.submission_count(copied.task.id) == 0
        new = uow.tasks.latest_version(copied.task.id)
    assert new is not None and new.statement == saved.version.statement


def test_copying_needs_a_free_name(database: Database, course) -> None:
    saved = _task(database, course, "ex1/p2", "ex1")
    with pytest.raises(AdminError, match="ex1/p2"):
        copy_task(database, task_id=saved.task.id, unit="ex1", name="p2")
    with pytest.raises(AdminError, match="名前"):
        copy_task(database, task_id=saved.task.id, unit="ex3", name=" ")


def test_a_title_that_was_the_key_follows_the_new_key(database: Database, course) -> None:
    """**見出しの無い課題の題名はキーそのもの**（`_title_of`）。移したら付いていく。"""
    saved = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(key="test4/echoClient", statement="見出しの無い本文", unit="test4"),
        subject_profile="cs_lang_c_intro",
        authored_by=TEACHER,
    )
    assert saved.task.title == "test4/echoClient"

    moved = move_task(database, task_id=saved.task.id, unit="test5", name="echoClient_comments.py")
    assert moved.task.title == "test5/echoClient_comments.py"

    copied = copy_task(database, task_id=moved.task.id, unit="test6", name="again")
    assert copied.task.title == "test6/again"


def test_a_title_the_instructor_chose_is_kept(database: Database, course) -> None:
    saved = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key="test4/echoClient", title="echoClient.py", statement="本文", unit="test4"
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=TEACHER,
    )
    moved = move_task(database, task_id=saved.task.id, unit="test5")
    assert moved.task.title == "echoClient.py"
