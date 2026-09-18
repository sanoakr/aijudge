"""提出物のファイル名が採点画面の表示を壊さないことを固定する。

ネットワーク演習の画像提出課題で、一部の提出だけが採点画面に出なかった。
共通点は macOS の日本語環境のスクリーンショット名（「スクリーンショット
2026-09-17 10.00.00.png」）── 空白ではなく**非 ASCII** が原因で、
`Content-Disposition` に埋めた名前が latin-1 に収まらず、ファイルを返す
経路が 500 になっていた。`<img>` は URL に artifact id しか使わないので、
画面のほうは 200 のまま画像だけ欠ける。
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
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
PASSWORD = "correct horse battery staple"
SCREENSHOT = "スクリーンショット 2026-09-17 10.00.00.png"
PAYLOAD = b"fake png bytes"


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    console = Console(database, store, profiles_dir=PROFILES)
    client = TestClient(create_app(console), raise_server_exceptions=False)
    submissions = SubmissionService(database.unit_of_work, store)

    with database.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="net1",
                title="ネットワーク及び演習",
                term="2026-後期",
                subject_profile="cs_lang_c_intro",
            )
        )
        uow.commit()

    version = TaskVersion(
        id=TaskVersionId(new_id("tsv")),
        task_id=TaskId(new_id("tsk")),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="設定画面のスクリーンショットを提出してください。",
        criteria=(
            RubricCriterion(
                id=CriterionId(new_id("crt")),
                code="shot",
                title="設定の内容",
                description="教員が見て採点する",
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
        created_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=version.task_id, course_id=COURSE, title="第1回"))
        uow.tasks.save_version(version)
        uow.commit()

    def register(login: str, role: Role) -> UserId:
        with database.unit_of_work() as uow:
            svc = AuthService(uow.identity, audit=uow.audit)
            p = svc.register(tenant_id=TENANT, login=login, display_name=login, password=PASSWORD)
            svc.enroll(tenant_id=TENANT, course_id=COURSE, user_id=p.user_id, role=role)
            uow.commit()
        return p.user_id

    learner = register("s2400001", Role.LEARNER)
    register("teacher", Role.INSTRUCTOR)
    res = client.post(
        "/auth/local", data={"login": "teacher", "password": PASSWORD}, follow_redirects=False
    )
    client.cookies.set(SESSION_COOKIE, res.cookies[SESSION_COOKIE])

    result = submissions.accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename=SCREENSHOT, kind=ArtifactKind.IMAGE, payload=PAYLOAD)],
    )
    # 人手採点の観点だけなので GradingRun は `awaiting_human` で止まる。
    # 採点画面はそれを待って開く（ADR 0007）。
    GradingWorker(
        database, store, profiles_dir=PROFILES, registry=EvaluatorRegistry().load_installed()
    ).run_until_empty()

    yield client, str(result.submission.id), str(result.submission.artifacts[0].id)
    database.dispose()


def test_an_image_named_like_a_macos_screenshot_is_shown_and_served(world) -> None:
    client, sub_id, art_id = world

    page = client.get(f"/review/{sub_id}/reveal")
    assert page.status_code == 200
    assert SCREENSHOT in page.text
    assert f"/review/{sub_id}/artifacts/{art_id}" in page.text

    served = client.get(f"/review/{sub_id}/artifacts/{art_id}")
    assert served.status_code == 200, served.text
    assert served.content == PAYLOAD
    assert served.headers["content-type"].startswith("image/png")
    disposition = served.headers["content-disposition"]
    assert disposition.startswith("inline;")
    # 拡張形式で名前が残り、`filename=` の側は ASCII に落ちている。
    assert "filename*=UTF-8''%E3%82%B9%E3%82%AF" in disposition
    assert 'filename=" 2026-09-17 10.00.00.png"' in disposition
