"""コースを消したあとに、そのコースを指す行が残らないこと（#156）。

**提出を消す経路はここだけ。** 学習者の提出があるコースは消せないので、
実際に消えるのは教員の動作確認（trial・#108）だけだが、提出を指す表は
7 つあり、**1 つ数え漏らすと存在しない提出を指す行が残る**。

だからテーブルの一覧を手で書かない。`submission_id` / `course_id` を持つ表を
**スキーマから走査して**、消したあとに 1 行も残っていないことを確かめる ──
表が増えたときに、このテストが先に落ちる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from aijudge_admin import AdminError, delete_course, ensure_course
from aijudge_core import ArtifactKind, Role
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_persistence import Database, schema
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
TEACHER = UserId("usr_" + "2" * 32)
AUTHOR = UserId("usr_" + "a" * 32)


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習",
        term="2026-前期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    yield database, store, course
    database.dispose()


def _task_version(database: Database, course_id: CourseId):
    """このコースに課題を 1 件置く（提出の宛先）。"""
    from aijudge_authoring.importers import sharif_judge

    version = sharif_judge.import_problem(
        REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task",
        course_id=course_id,
        subject_profile="cs_lang_c_intro",
        authored_by=AUTHOR,
        readability_weight=0.3,
    )
    from aijudge_core import Task

    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=version.task_id, course_id=course_id, title="例題"))
        uow.tasks.save_version(version)
        uow.commit()
    return version


def _submit(database: Database, store, version, learner: UserId, *, trial: bool):
    service = SubmissionService(database.unit_of_work, store)
    return service.accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
        submitted_as=Role.INSTRUCTOR if trial else Role.LEARNER,
    )


def test_a_course_with_learner_submissions_is_refused(world) -> None:
    """1 件でもあれば消さない ── 成績が何のコースの点なのか辿れなくなる。"""
    database, store, course = world
    version = _task_version(database, course.id)
    _submit(database, store, version, LEARNER, trial=False)

    with pytest.raises(AdminError, match="学習者の提出"):
        delete_course(database, course_id=course.id)

    with database.unit_of_work() as uow:
        assert uow.identity.get_course(course.id) is not None, "拒否したのに消えている"


def test_a_course_with_only_trials_can_be_deleted(world) -> None:
    """教員の動作確認（#108）は成績にも統計にも数えない。失う記録が無い。"""
    database, store, course = world
    version = _task_version(database, course.id)
    accepted = _submit(database, store, version, TEACHER, trial=True)
    assert accepted.submission.is_trial

    report = delete_course(database, course_id=course.id, artifact_store=store)

    assert report.trial_submissions == 1
    assert report.tasks == 1
    with database.unit_of_work() as uow:
        assert uow.identity.get_course(course.id) is None


def test_nothing_that_pointed_at_the_course_survives(world) -> None:
    """**表を手で並べない。** スキーマを走査して、残りが 0 であることを見る。

    `submission_id` / `course_id` を持つ表が増えたとき、削除の側で数え
    漏らせばここで落ちる。
    """
    database, store, course = world
    version = _task_version(database, course.id)
    accepted = _submit(database, store, version, TEACHER, trial=True)
    submission_id = str(accepted.submission.id)

    delete_course(database, course_id=course.id, artifact_store=store)

    leftovers: list[str] = []
    for table in schema.Base.metadata.tables.values():
        for name, value in (("submission_id", submission_id), ("course_id", str(course.id))):
            column = table.columns.get(name)
            if column is None:
                continue
            with database.session() as session:
                remaining = int(
                    session.execute(
                        select(func.count()).select_from(table).where(column == value)
                    ).scalar_one()
                )
            if remaining:
                leftovers.append(f"{table.name}.{name}: {remaining} 行")
    assert not leftovers, "消したコース・提出を指す行が残っている: " + ", ".join(leftovers)


def test_the_course_row_and_its_enrolments_are_gone(world) -> None:
    database, _, course = world
    with database.unit_of_work() as uow:
        from aijudge_identity import AuthService

        AuthService(uow.identity).enroll(
            tenant_id=TENANT, course_id=course.id, user_id=LEARNER, role=Role.LEARNER
        )
        uow.commit()

    delete_course(database, course_id=course.id)

    with database.unit_of_work() as uow:
        assert uow.identity.get_course(course.id) is None
        assert uow.identity.list_enrollments(course.id) == ()


def test_the_trial_artifacts_are_removed_from_the_store(world) -> None:
    """消した提出の成果物も消す（ストアが削除に対応していれば）。"""
    database, store, course = world
    version = _task_version(database, course.id)
    accepted = _submit(database, store, version, TEACHER, trial=True)
    keys = [artifact.storage_key for artifact in accepted.submission.artifacts]
    assert keys and all(store.exists(key) for key in keys)

    delete_course(database, course_id=course.id, artifact_store=store)

    assert not any(store.exists(key) for key in keys)


def test_deleting_a_course_that_does_not_exist_is_refused(world) -> None:
    database, _, _ = world
    with pytest.raises(AdminError, match="ありません"):
        delete_course(database, course_id=CourseId("crs_" + "9" * 32))
