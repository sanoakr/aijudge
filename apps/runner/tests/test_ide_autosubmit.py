"""受付終了時の自動提出（設計書 §9.1）。

固定したいのは次のこと。

出す      一度も提出していない課題と、最後の提出のあとに書き換えた課題。
出さない  受付中の課題・空の保存・学習者ではない人の保存・受け付けない形式。
冪等      何度走らせても同じ。本人が出した内容はもう一度出さない。
時刻      提出時刻は自動保存の時刻（走った時刻にすると遅延として減点される）。
印        自動の提出は `auto_close` として残り、本人の提出と区別できる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import AnswerMode, ArtifactKind, Course, Enrollment, Role, Task
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_ide import SubmissionOrigin, make_buffer
from aijudge_persistence import Database
from aijudge_runner.autosubmit import close_editor_tasks
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
TEACHER = UserId("usr_" + "4" * 32)
AUTHOR = UserId("usr_" + "3" * 32)

OPENS = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
DUE = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
CLOSES = datetime(2026, 9, 24, 10, 30, tzinfo=UTC)
AFTER = CLOSES + timedelta(minutes=1)
SOURCE = "int main(void){return 0;}\n"


class World:
    def __init__(self, tmp_path: Path, **task_update) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.version = sharif_judge.import_problem(
            EXAMPLE_TASK, course_id=COURSE, subject_profile="cs_lang_c_intro", authored_by=AUTHOR
        )
        self.task = Task(
            id=self.version.task_id,
            course_id=COURSE,
            title="最大・最小・平均",
            unit="ex01",
            answer_mode=AnswerMode.EDITOR,
            accepted_suffixes=(".c", ".md"),
            opens_at=OPENS,
            due_at=DUE,
            accepts_until=CLOSES,
            **task_update,
        )
        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="prog2",
                    title="プログラミング演習 II",
                    term="2026-後期",
                    subject_profile="cs_lang_c_intro",
                )
            )
            for user, role in ((LEARNER, Role.LEARNER), (TEACHER, Role.INSTRUCTOR)):
                uow.identity.save_enrollment(
                    Enrollment(tenant_id=TENANT, course_id=COURSE, user_id=user, role=role)
                )
            uow.tasks.save_task(self.task)
            uow.tasks.save_version(self.version)
            uow.commit()

    def autosave(self, source: str = SOURCE, *, at: datetime, suffix: str = ".c", who=LEARNER):
        with self.database.unit_of_work() as uow:
            uow.ide_buffers.save(
                make_buffer(
                    tenant_id=TENANT,
                    learner_id=who,
                    task_id=self.task.id,
                    suffix=suffix,
                    source=source,
                    now=at,
                )
            )
            uow.commit()

    def submit_by_hand(self, source: str, *, at: datetime):
        service = SubmissionService(self.database.unit_of_work, self.store, clock=lambda: at)
        return service.accept(
            tenant_id=TENANT,
            task_version_id=self.version.id,
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            files=[
                IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=source.encode())
            ],
        )

    def close(self, now: datetime = AFTER, **kw):
        return close_editor_tasks(self.database, self.store, now=now, **kw)

    def submissions(self):
        with self.database.unit_of_work() as uow:
            return uow.submissions.list_for_learner(TENANT, LEARNER)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_an_unsubmitted_autosave_is_submitted_at_its_own_time(world: World) -> None:
    saved_at = CLOSES - timedelta(minutes=3)
    world.autosave(at=saved_at)

    report = world.close()

    assert report.submitted == 1
    (submission,) = world.submissions()
    # **提出時刻は自動保存の時刻。** 走った時刻（受付終了の 1 分後）ではない。
    assert submission.submitted_at == saved_at
    (artifact,) = submission.artifacts
    assert artifact.filename == "main.c" and artifact.kind is ArtifactKind.CODE
    with world.database.unit_of_work() as uow:
        link = uow.ide_links.for_submission(submission.id)
        assert uow.jobs.pending_count() == 1
    assert link is not None and link.origin is SubmissionOrigin.AUTO_CLOSE


def test_running_twice_submits_once(world: World) -> None:
    world.autosave(at=CLOSES - timedelta(minutes=3))

    world.close()
    again = world.close(AFTER + timedelta(minutes=1))

    assert again.submitted == 0 and again.unchanged == 1
    assert len(world.submissions()) == 1


def test_what_the_learner_already_submitted_is_not_submitted_again(world: World) -> None:
    """本人が同じ内容を出していれば新しい提出にしない。**「自動」の印も付けない。**"""
    world.submit_by_hand(SOURCE, at=CLOSES - timedelta(minutes=10))
    world.autosave(at=CLOSES - timedelta(minutes=10))

    report = world.close()

    assert report.unchanged == 1 and report.submitted == 0
    (submission,) = world.submissions()
    with world.database.unit_of_work() as uow:
        assert uow.ide_links.for_submission(submission.id) is None


def test_changes_after_the_last_submission_are_submitted(world: World) -> None:
    world.submit_by_hand(SOURCE, at=CLOSES - timedelta(minutes=20))
    world.autosave("int main(void){return 1;}\n", at=CLOSES - timedelta(minutes=2))

    report = world.close()

    assert report.submitted == 1
    assert len(world.submissions()) == 2


def test_a_report_is_submitted_as_text(world: World) -> None:
    world.autosave("# 考察\n", at=CLOSES - timedelta(minutes=1), suffix=".md")

    world.close()

    (submission,) = world.submissions()
    (artifact,) = submission.artifacts
    assert artifact.filename == "answer.md" and artifact.kind is ArtifactKind.MARKDOWN


def test_nothing_happens_while_the_set_is_still_open(world: World) -> None:
    world.autosave(at=CLOSES - timedelta(minutes=5))

    report = world.close(now=CLOSES - timedelta(minutes=1))

    assert report.submitted == 0
    assert world.submissions() == ()


def test_a_set_that_closed_long_ago_is_left_alone(world: World) -> None:
    world.autosave(at=CLOSES - timedelta(minutes=5))

    report = world.close(now=CLOSES + timedelta(days=2), lookback_hours=24)

    assert report.submitted == 0


@pytest.mark.parametrize(
    ("source", "who", "suffix", "reason"),
    [
        ("   \n", LEARNER, ".c", "空"),
        (SOURCE, TEACHER, ".c", "学習者ではない"),
        (SOURCE, LEARNER, ".py", "課題が受け付けない形式"),
    ],
)
def test_some_autosaves_are_not_submitted(
    world: World, source: str, who: UserId, suffix: str, reason: str
) -> None:
    world.autosave(source, at=CLOSES - timedelta(minutes=1), who=who, suffix=suffix)

    report = world.close()

    assert report.submitted == 0
    assert report.skipped == {reason: 1}


def test_an_upload_task_is_never_auto_submitted(tmp_path: Path) -> None:
    world = World(tmp_path, **{})
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(world.task.model_copy(update={"answer_mode": AnswerMode.UPLOAD}))
        uow.commit()
    world.autosave(at=CLOSES - timedelta(minutes=1))

    assert world.close().submitted == 0


def test_a_dry_run_submits_nothing(world: World) -> None:
    world.autosave(at=CLOSES - timedelta(minutes=1))

    report = world.close(dry_run=True)

    assert report.submitted == 1
    assert world.submissions() == ()
