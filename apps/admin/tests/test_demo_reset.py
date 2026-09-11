"""デモコースのリセット（#194）。

**器ごと作り直す。** 中身だけ空にするのではない ── 当初の問題セットも
戻すので、作り直すほうが「学期の初めの状態」に一致する。

これができるのは**コースの ID が決定的だから**である。消して作り直しても
同じ ID になるので、学生が開いていた URL は死なない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin.demo_reset import reset_demo_course
from aijudge_admin.demo_seed import seed_demo_course
from aijudge_admin.operations import AdminError
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Enrollment,
    Role,
    Submission,
    SubmissionState,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TenantId, UserId
from aijudge_identity import AuthService, DemoCourse
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)
TEACHER = UserId("usr_" + "a" * 32)
LEARNER = UserId("usr_" + "b" * 32)
NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/demo.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    """**定義から作る。** リセットが戻すのと同じ素性でないと、比べる意味が無い
    （`subjects/demo/course.yaml`）。"""
    return seed_demo_course(
        database, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=TEACHER
    ).course


def _populate(database: Database, course) -> None:
    """使われた状態にする ── 受講登録と提出を足す。

    課題は定義が既に 3 件入れている（`seed_demo_course`）。
    """
    with database.unit_of_work() as uow:
        version = uow.tasks.latest_version(uow.tasks.list_for_course(course.id)[0].id)
        uow.identity.save_enrollment(
            Enrollment(tenant_id=TENANT, course_id=course.id, user_id=LEARNER, role=Role.LEARNER)
        )
        uow.submissions.save(
            Submission(
                id=SubmissionId("sub_" + "1" * 32),
                task_version_id=version.id,
                learner_id=LEARNER,
                # **デモの提出は trial**（#194 の B）。ここが偽だと、消す前に
                # `delete_course` が拒む。
                is_demo=True,
                state=SubmissionState.SUBMITTED,
                submitted_at=NOW,
                artifacts=(
                    Artifact(
                        id=ArtifactId("art_" + "1" * 32),
                        submission_id=SubmissionId("sub_" + "1" * 32),
                        role=ArtifactRole.ORIGINAL,
                        kind=ArtifactKind.CODE,
                        filename="main.c",
                        storage_key="demo/main.c",
                        content_hash="0" * 64,
                        byte_size=10,
                        created_at=NOW,
                    ),
                ),
                created_at=NOW,
            )
        )
        uow.commit()


def _reset(database: Database, course, tenant_id: TenantId = TENANT):
    return reset_demo_course(
        database,
        DemoCourse(course_id=course.id),
        tenant_id=tenant_id,
        profiles_dir=PROFILES,
        authored_by=TEACHER,
    )


def test_the_course_keeps_its_id_so_open_urls_survive(database: Database, course) -> None:
    """**作り直しても ID は同じ。**

    ID はテナント・コード・学期から導かれる（`course_id_for`）。学生が開いて
    いた URL が死なないのはこの性質による ── 「中身だけ空にする」案の利点
    として挙げたものが、作り直しでもそのまま得られる。
    """
    _populate(database, course)
    result = _reset(database, course)

    assert result.course.id == course.id
    assert result.course.title == course.title
    assert result.course.subject_profile == course.subject_profile


def test_submissions_tasks_and_enrolments_all_go(database: Database, course) -> None:
    """提出・課題・受講登録がすべて消える。

    **受講登録も消す。** 次のログインで自動的に戻るので誰も締め出されない
    （`enrol_into_demo_course`）。
    """
    _populate(database, course)
    result = _reset(database, course)

    assert (result.submissions, result.enrolments) == (1, 1)
    assert result.tasks == 3, "定義の課題が消えていない"
    # **課題は戻る**（#194）── リセットは「学期の初めの状態」であって、
    # 空のコースではない。
    assert result.seeded_tasks == 3
    with database.unit_of_work() as uow:
        assert uow.submissions.list_for_course(course.id) == ()
        assert uow.identity.list_enrollments(course.id) == ()
        assert len(uow.tasks.list_for_course(course.id)) == 3


def test_logging_in_again_puts_the_learner_back(database: Database, course, monkeypatch) -> None:
    """**リセットは締め出しではない。** 次のログインで戻る。"""
    _populate(database, course)
    _reset(database, course)

    monkeypatch.setenv("AIJUDGE_DEMO_COURSE", str(course.id))
    with database.unit_of_work() as uow:
        service = AuthService(uow.identity, audit=uow.audit)
        service.register(
            tenant_id=TENANT,
            login="y2400001",
            display_name="学生",
            password="correct horse battery",
        )
        uow.commit()
    with database.unit_of_work() as uow:
        AuthService(uow.identity, audit=uow.audit).login(
            tenant_id=TENANT, login="y2400001", password="correct horse battery"
        )
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.identity.list_enrollments(course.id), "戻っていない"


def test_it_refuses_a_course_from_another_tenant(database: Database, course) -> None:
    """**打ち間違いで本物のコースを消さない。**"""
    with pytest.raises(AdminError):
        _reset(database, course, tenant_id=OTHER_TENANT)
    with database.unit_of_work() as uow:
        assert uow.identity.get_course(course.id) is not None


def test_it_refuses_a_course_that_is_not_there(database: Database) -> None:
    """環境変数に古い ID が残っている運用は普通に起きる。"""
    from aijudge_core.ids import CourseId

    with pytest.raises(AdminError):
        reset_demo_course(
            database,
            DemoCourse(course_id=CourseId("crs_" + "0" * 32)),
            tenant_id=TENANT,
            profiles_dir=PROFILES,
            authored_by=TEACHER,
        )
