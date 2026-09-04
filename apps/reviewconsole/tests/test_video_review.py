"""教員コンソールが動画を Range 対応で配信することを固定する。

教員は動画を視聴して `__human__` 観点を採点する（`GradingRun.awaiting_human`）。
シークに 206 が要るので、`<video>` からの Range リクエストを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_core import (
    HUMAN_SCORED,
    ArtifactKind,
    Course,
    Provenance,
    ReviewState,
    Role,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import CourseId, CriterionId, TaskId, TaskVersionId, TenantId, UserId
from aijudge_grader import GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
PASSWORD = "correct horse battery staple"


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    video_store = FilesystemArtifactStore(tmp_path / "video")
    console = Console(database, store, profiles_dir=PROFILES, video_store=video_store)
    client = TestClient(create_app(console))
    submissions = SubmissionService(database.unit_of_work, store, stream_store=video_store)

    with database.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="media1",
                title="メディア演習",
                term="2026-前期",
                subject_profile="cs_intro_c",
            )
        )
        uow.commit()

    version = TaskVersion(
        id=TaskVersionId(new_id("tsv")),
        task_id=TaskId(new_id("tsk")),
        version=1,
        subject_profile="cs_intro_c",
        statement="デモ動画を提出してください。",
        criteria=(
            RubricCriterion(
                id=CriterionId(new_id("crt")),
                code="demo",
                title="デモの内容",
                description="教員が視聴して採点する",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="不可", descriptor="未達", score_ratio=0.0),
                    RubricLevel(level=3, label="達成", descriptor="達成", score_ratio=1.0),
                ),
                evaluator_id=HUMAN_SCORED,
            ),
        ),
        max_score=100.0,
        provenance=Provenance(
            authored_by=UserId("usr_" + "a" * 32), review_state=ReviewState.APPROVED
        ),
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=version.task_id, course_id=COURSE, title="デモ"))
        uow.tasks.save_version(version)
        uow.commit()

    def register(login: str, role: Role) -> UserId:
        with database.unit_of_work() as uow:
            svc = AuthService(uow.identity)
            p = svc.register(tenant_id=TENANT, login=login, display_name=login, password=PASSWORD)
            svc.enroll(tenant_id=TENANT, course_id=COURSE, user_id=p.user_id, role=role)
            uow.commit()
        return p.user_id

    learner = register("s2400001", Role.LEARNER)
    register("teacher", Role.INSTRUCTOR)
    res = client.post(
        "/login", data={"login": "teacher", "password": PASSWORD}, follow_redirects=False
    )
    client.cookies.set(SESSION_COOKIE, res.cookies[SESSION_COOKIE])

    result = submissions.accept_stream(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner,
        subject_profile="cs_intro_c",
        filename="demo.mp4",
        kind=ArtifactKind.VIDEO,
        chunks=iter([bytes(range(256))]),
        max_bytes=1_000_000,
    )
    artifact_id = str(result.submission.artifacts[0].id)

    # 採点を通す ── 動画観点は `__human__` なので `awaiting_human` に入り、
    # GradingRun ができる（総点は withheld）。教員はここから視聴して採点する。
    worker = GradingWorker(
        database, store, profiles_dir=PROFILES, registry=EvaluatorRegistry().load_installed()
    )
    worker.run_until_empty()

    with database.unit_of_work() as uow:
        run = uow.runs.latest_for(result.submission.id)
    assert run is not None
    assert run.awaiting_human  # 動画観点は人待ち

    yield client, str(result.submission.id), artifact_id
    database.dispose()


def test_instructor_can_stream_the_video_with_ranges(world) -> None:
    client, sub_id, art_id = world
    url = f"/review/{sub_id}/artifacts/{art_id}"

    full = client.get(url)
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.content == bytes(range(256))
    assert full.headers.get("content-type", "").startswith("video/mp4")

    part = client.get(url, headers={"Range": "bytes=100-149"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 100-149/256"
    assert part.content == bytes(range(100, 150))
