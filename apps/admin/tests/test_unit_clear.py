"""問題セットを丸ごと片付ける（#59）。

**削除と取り下げを 1 操作で振り分ける。** セットの中に提出のある課題と
無い課題が混ざるのが普通で、片方に揃えると「消せるはずの打ち間違いが残る」か
「消してはいけない成績の出所が消える」かのどちらかになる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin import ensure_course
from aijudge_admin.authoring import save_task
from aijudge_admin.operations import AdminError
from aijudge_admin.tasks import clear_unit
from aijudge_authoring import TaskSpec
from aijudge_core import Submission, SubmissionState
from aijudge_core.ids import SubmissionId, TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/unit.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    return obj


def _task(database: Database, course, key: str, unit: str, position: int):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key=key,
            statement=f"## [必須] {key} ##\n\n本文",
            unit=unit,
            position=position,
            readability_weight=0.3,
        ),
        subject_profile="cs_intro_c",
        authored_by=TEACHER,
    )


def _submit(database: Database, saved, suffix: str = "a") -> None:
    with database.unit_of_work() as uow:
        uow.submissions.save(
            # 下書きで足りる。**数えているのは「使われたか」**であって
            # 採点まで通ったかではない（`submission_count`）。
            Submission(
                id=SubmissionId("sub_" + suffix * 32),
                task_version_id=saved.version.id,
                learner_id=LEARNER,
                state=SubmissionState.DRAFT,
                attempt=1,
                created_at=datetime.now(UTC),
            )
        )
        uow.commit()


def test_used_tasks_are_withdrawn_and_unused_ones_deleted(database: Database, course) -> None:
    """**混ざっているのが普通。** 提出のある課題だけ残して出さなくする。"""
    used = _task(database, course, "ex03/p1", "ex03", 1)
    unused = _task(database, course, "ex03/p2", "ex03", 2)
    _submit(database, used)

    report = clear_unit(database, course_id=course.id, unit="ex03")

    assert [t.id for t in report.withdrawn] == [used.task.id]
    assert [t.id for t in report.deleted] == [unused.task.id]

    with database.unit_of_work() as uow:
        # 使われたものは残り、学習者には出ない。
        kept = uow.tasks.get_task(used.task.id)
        assert kept is not None and kept.withdrawn
        # 使われていないものは消える。
        assert uow.tasks.get_task(unused.task.id) is None


def test_a_dry_run_changes_nothing(database: Database, course) -> None:
    """内訳を出すためだけの経路。**ここが壊れると「確認したら消えていた」。**"""
    used = _task(database, course, "ex04/p1", "ex04", 1)
    unused = _task(database, course, "ex04/p2", "ex04", 2)
    _submit(database, used)

    report = clear_unit(database, course_id=course.id, unit="ex04", dry_run=True)

    assert len(report.deleted) == 1 and len(report.withdrawn) == 1 and report.total == 2
    with database.unit_of_work() as uow:
        assert uow.tasks.get_task(unused.task.id) is not None, "dry-run で消えた"
        kept = uow.tasks.get_task(used.task.id)
        assert kept is not None and not kept.withdrawn, "dry-run で取り下げられた"


def test_already_withdrawn_tasks_are_not_counted_as_withdrawn(database: Database, course) -> None:
    """**取り下げ済みを「取り下げた」と数えない。** 何が変わったかが言えなくなる。"""
    used = _task(database, course, "ex05/p1", "ex05", 1)
    _submit(database, used)
    clear_unit(database, course_id=course.id, unit="ex05")

    again = clear_unit(database, course_id=course.id, unit="ex05")
    assert not again.withdrawn
    assert [t.id for t in again.untouched] == [used.task.id]


def test_an_empty_unit_is_refused(database: Database, course) -> None:
    """**問題セットの取り違えに気づける。** 黙って 0 件成功にしない。"""
    with pytest.raises(AdminError):
        clear_unit(database, course_id=course.id, unit="ex99")
