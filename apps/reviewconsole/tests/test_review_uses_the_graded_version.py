"""確定の画面は、提出の版ではなく**採点記録の版**の観点で段階を出す（2026-11-13）。

課題を直したあとの再採点は今の版で走る。exam08 p5 では fix_scope を AI の 4 段（版 1）から
決定的な 2 段（版 2）に移して再採点した。画面は提出の版 1 の段階を出し、教員が選んだ
段階 2 を、確定の計算（記録の版 2 を引く）が見つけられずに 500 になった。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_core import (
    ArtifactKind,
    Course,
    GradingRun,
    Provenance,
    Role,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_identity import AuthService
from aijudge_persistence import Database, ObservationFileStore
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
PROFILE = "cs_lang_c_intro"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
TASK = TaskId("tsk_" + "2" * 32)
V1 = TaskVersionId("tsv_" + "3" * 32)
V2 = TaskVersionId("tsv_" + "4" * 32)
OLD_CRITERION = CriterionId("crt_" + "5" * 32)
NEW_CRITERION = CriterionId("crt_" + "6" * 32)
PASSWORD = "correct horse battery"
JUSTIFICATION = "直した範囲を確認しました。判定は妥当です。"


def _level(level: int, ratio: float) -> RubricLevel:
    return RubricLevel(
        level=level, label=f"段階{level}", descriptor=f"段階{level}", score_ratio=ratio
    )


def _version(version_id: TaskVersionId, number: int, criterion: RubricCriterion) -> TaskVersion:
    return TaskVersion(
        id=version_id,
        task_id=TASK,
        version=number,
        subject_profile=PROFILE,
        statement="誤りを 4 か所直す",
        criteria=(criterion,),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "8" * 32)),
        created_at=NOW,
    )


# 版 1: AI が 4 段で判定していた観点。
OLD = RubricCriterion(
    id=OLD_CRITERION,
    code="fix_scope",
    title="誤りの直し方",
    description="直した範囲",
    weight=1.0,
    evaluator_id="rubric_ai_judge",
    levels=(_level(0, 0.0), _level(1, 0.34), _level(2, 0.67), _level(3, 1.0)),
)
# 版 2: 同じ観点を決定的な 2 段に移した。
NEW = RubricCriterion(
    id=NEW_CRITERION,
    code="fix_scope",
    title="誤りの直し方",
    description="直した行数",
    weight=1.0,
    evaluator_id="line_diff_check",
    levels=(_level(0, 0.0), _level(1, 1.0)),
)


def _run_on_v2(submission_id: str) -> GradingRun:
    """版 2 で再採点した記録（提出は版 1 を指したまま）。"""
    return GradingRun.model_validate(
        {
            "id": "grn_" + "9" * 32,
            "submission_id": submission_id,
            "context": {
                "task_version_id": str(V2),
                "subject_profile": PROFILE,
                "rubric_version": "r@2",
                "input_hash": "sha256:x",
                "pipeline_version": "1",
            },
            "evaluator_results": [
                {
                    "id": "evr_" + "a" * 32,
                    "evaluator_id": "line_diff_check",
                    "kind": "deterministic",
                    "raw_output": {},
                }
            ],
            "criterion_scores": [
                {
                    "id": "cs_" + "b" * 32,
                    "criterion_id": str(NEW_CRITERION),
                    "evaluator_result_id": "evr_" + "a" * 32,
                    "kind": "deterministic",
                    "level": 1,
                    "score_ratio": 1.0,
                    "weight": 1.0,
                    "confidence": 1.0,
                    "conclusive": True,
                    "rationale": "変えた行は 4 行で、上限以内です。",
                }
            ],
            "score_ratio": 1.0,
            "confidence": 1.0,
            "routing": "review_required",
            "created_at": "2026-11-13T00:14:00Z",
        }
    )


@pytest.fixture
def client(tmp_path: Path):
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    console = Console(
        database,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        profiles_dir=PROFILES,
        observations=ObservationFileStore(tmp_path / "observations"),
    )
    console._rates[PROFILE] = 0.0  # blind 抽出に当てない
    with database.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="prog2",
                title="プログラミング演習 II",
                term="2026-後期",
                subject_profile=PROFILE,
            )
        )
        uow.tasks.save_task(Task(id=TASK, course_id=COURSE, title="exam08 p5"))
        uow.tasks.save_version(_version(V1, 1, OLD))
        uow.tasks.save_version(_version(V2, 2, NEW))
        auth = AuthService(uow.identity, audit=uow.audit)
        learner = auth.register(
            tenant_id=TENANT, login="s2600001", display_name="s2600001", password=PASSWORD
        )
        auth.enroll(tenant_id=TENANT, course_id=COURSE, user_id=learner.user_id, role=Role.LEARNER)
        teacher = auth.register(
            tenant_id=TENANT, login="instructor", display_name="instructor", password=PASSWORD
        )
        auth.enroll(
            tenant_id=TENANT, course_id=COURSE, user_id=teacher.user_id, role=Role.INSTRUCTOR
        )
        uow.commit()

    # 提出は版 1 を指す（直す前に出した）。
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    accepted = SubmissionService(database.unit_of_work, store).accept(
        tenant_id=TENANT,
        task_version_id=V1,
        learner_id=learner.user_id,
        subject_profile=PROFILE,
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}\n")],
    )
    submission_id = str(accepted.submission.id)
    run = _run_on_v2(submission_id)
    with database.unit_of_work() as uow:
        uow.runs.save(run)
        uow.commit()

    test_client = TestClient(create_app(console))
    response = test_client.post(
        "/auth/local", data={"login": "instructor", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    test_client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])
    yield test_client, database, submission_id, run.id
    database.dispose()


def test_the_page_offers_the_levels_of_the_version_that_was_graded(client) -> None:
    test_client, _, submission_id, _ = client

    page = test_client.get(f"/review/{submission_id}/reveal")

    assert page.status_code == 200, page.text
    # 版 2 の 2 段だけ。版 1 にしか無い段階 2・3 を選ばせない。
    offered = re.findall(r'name="level_fix_scope"[^>]*?value="(\d+)"', page.text, re.DOTALL)
    assert sorted(set(offered)) == ["0", "1"]


def test_finalizing_on_the_graded_version_succeeds(client) -> None:
    test_client, database, submission_id, run_id = client

    response = test_client.post(
        f"/review/{submission_id}/finalize",
        data={"level_fix_scope": "0", "comment": JUSTIFICATION},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    with database.unit_of_work() as uow:
        review = uow.reviews.find_review_for_run(run_id)
    assert review is not None
    assert set(review.adjusted_levels) == {NEW_CRITERION}


def test_a_level_only_the_old_version_had_is_refused_not_a_500(client) -> None:
    """古い画面を開いたままの送信でも、500 にしない。"""
    test_client, _, submission_id, _ = client

    response = test_client.post(
        f"/review/{submission_id}/finalize",
        data={"level_fix_scope": "2", "comment": JUSTIFICATION},
        follow_redirects=False,
    )

    assert response.status_code == 400, response.text
