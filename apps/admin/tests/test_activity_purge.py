"""保存期間を過ぎた IDE の作業の記録と自動保存を消す（ADR 0023 §5）。

固定したいのは次のこと。

期限       動画と同じ。締切から 6 ヶ月、締切が無ければ 1 年（開いた時刻・保存時刻から）。
下見       `plan` は何も消さない。
順序       本体（ファイル）を消してから索引。置き場所が無ければ作業の記録は消さない。
自動保存   同じ期間で消す（学習者のコードである）。
記録       消したことは監査ログに残る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_admin.activity_purge import plan_activity_purge, purge_activity
from aijudge_core import Course, Enrollment, Role, Task
from aijudge_core.ids import CourseId, TaskId, TenantId, UserId, new_id
from aijudge_ide import ActivityFiles, EventBatch, IdeSession, IdeSessionId, make_buffer
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
DUE = datetime(2026, 1, 20, 10, 0, tzinfo=UTC)
CLOSES = DUE + timedelta(minutes=30)
EXAM = TaskId("tsk_" + "a" * 32)
SANDBOX = TaskId("tsk_" + "b" * 32)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.root = tmp_path / "activity"
        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="prog2",
                    title="演習",
                    term="2025-後期",
                    subject_profile="cs_lang_c_intro",
                )
            )
            uow.identity.save_enrollment(
                Enrollment(tenant_id=TENANT, course_id=COURSE, user_id=LEARNER, role=Role.LEARNER)
            )
            # 締切のある試験の問題セットと、締切の無い自習の問題セット。
            uow.tasks.save_task(
                Task(
                    id=EXAM,
                    course_id=COURSE,
                    title="試験 1",
                    unit="exam",
                    due_at=DUE,
                    accepts_until=CLOSES,
                )
            )
            uow.tasks.save_task(Task(id=SANDBOX, course_id=COURSE, title="自習", unit="free"))
            uow.commit()

    def session(self, unit: str, started_at: datetime) -> IdeSession:
        session = IdeSession(
            id=IdeSessionId(new_id("ide")),
            tenant_id=TENANT,
            learner_id=LEARNER,
            course_id=COURSE,
            unit=unit,
            started_at=started_at,
            consented_at=started_at,
        )
        path, digest, size = ActivityFiles(self.root).write_batch(
            session, 0, [{"type": "hello", "t": 0}], {}
        )
        with self.database.unit_of_work() as uow:
            uow.ide_activity.start_session(session)
            uow.ide_activity.add_batch(
                EventBatch(
                    ide_session_id=session.id,
                    seq=0,
                    received_at=started_at,
                    event_count=1,
                    snapshot_count=0,
                    byte_size=size,
                    sha256=digest,
                    path=path,
                )
            )
            uow.commit()
        return session

    def autosave(self, task_id: TaskId, at: datetime) -> None:
        with self.database.unit_of_work() as uow:
            uow.ide_buffers.save(
                make_buffer(
                    tenant_id=TENANT,
                    learner_id=LEARNER,
                    task_id=task_id,
                    suffix=".c",
                    source="int main(){}",
                    now=at,
                )
            )
            uow.commit()

    def plan(self, now: datetime):
        return plan_activity_purge(self.database, tenant_id=TENANT, now=now)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_deadline_starts_the_six_months(world: World) -> None:
    exam = world.session("exam", DUE - timedelta(hours=1))

    assert world.plan(DUE + timedelta(days=180)).sessions == ()
    plan = world.plan(DUE + timedelta(days=190))

    assert [s.id for s in plan.sessions] == [exam.id]
    assert plan.by_unit == (("prog2 / exam", 1),)


def test_without_a_deadline_a_year_from_opening(world: World) -> None:
    opened = datetime(2026, 3, 1, tzinfo=UTC)
    world.session("free", opened)

    assert world.plan(opened + timedelta(days=360)).sessions == ()
    plan = world.plan(opened + timedelta(days=370))
    assert len(plan.sessions) == 1 and plan.sessions_without_deadline == 1


def test_autosaves_go_a_month_after_the_close(world: World) -> None:
    """自動保存は**受付終了から 1 ヶ月**（作業の記録の 6 ヶ月より短い）。

    終了時点の最新は自動提出で提出になっているので、残すのは複製である。
    受付終了の無い課題（自習）は最後の保存から 1 年のまま。
    """
    world.autosave(EXAM, DUE - timedelta(minutes=5))
    world.autosave(SANDBOX, CLOSES)

    assert world.plan(CLOSES + timedelta(days=20)).buffers == ()
    plan = world.plan(CLOSES + timedelta(days=32))

    assert plan.buffers == ((LEARNER, EXAM),)
    assert world.plan(CLOSES + timedelta(days=370)).buffers == (
        (LEARNER, EXAM),
        (LEARNER, SANDBOX),
    )


def test_the_plan_deletes_nothing(world: World) -> None:
    world.session("exam", DUE)
    world.plan(DUE + timedelta(days=400))
    with world.database.unit_of_work() as uow:
        assert len(uow.ide_activity.course_sessions(COURSE)) == 1


def test_purging_removes_files_then_index_and_is_audited(world: World) -> None:
    exam = world.session("exam", DUE - timedelta(hours=1))
    kept = world.session("free", DUE)
    world.autosave(EXAM, DUE - timedelta(minutes=5))
    plan = world.plan(DUE + timedelta(days=190))

    outcome = purge_activity(world.database, plan, activity_dir=world.root, tenant_id=TENANT)

    assert (outcome.sessions, outcome.buffers, outcome.failed) == (1, 1, ())
    assert not ActivityFiles(world.root).session_dir(exam).exists()
    assert ActivityFiles(world.root).session_dir(kept).exists()
    with world.database.unit_of_work() as uow:
        assert [s.id for s in uow.ide_activity.course_sessions(COURSE)] == [kept.id]
        assert uow.ide_buffers.get(LEARNER, EXAM) is None
        actions = [e.action.value for e in uow.audit.list_recent(TENANT, limit=10)]
    assert "activity.purged" in actions


def test_without_the_directory_the_record_is_not_touched(world: World) -> None:
    """本体を消せないのに索引だけ消すと、誰にも辿れないファイルが残る。"""
    exam = world.session("exam", DUE - timedelta(hours=1))
    world.autosave(EXAM, DUE - timedelta(minutes=5))
    plan = world.plan(DUE + timedelta(days=190))

    outcome = purge_activity(world.database, plan, activity_dir=None, tenant_id=TENANT)

    assert outcome.sessions == 0 and outcome.failed == (str(exam.id),)
    # 自動保存は DB だけなので消える。
    assert outcome.buffers == 1
    with world.database.unit_of_work() as uow:
        assert uow.ide_activity.get_session(exam.id) is not None
