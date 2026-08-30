"""生成課題のレビュー画面の規則を固定する（S2、設計方針 §5）。

固定したいのは 5 つ。

TA には開けない    課題を出題可能にする操作は教員の権限（`_require_instructor`）。
他コースを探れない  存在と権限を区別しない。
理由なく却下できない 作問改善の材料であり、承認率の分母でもある。
知識要素を必ず出す  機械が検証していないので、見せなければ誰も気づかない。
二度は決められない  やり直しは新しい版から（P8）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_authoring.verification import GateOutcome, TaskChecks, VerificationReport
from aijudge_core import (
    Course,
    Provenance,
    QMatrixEntry,
    ReviewState,
    Role,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    KcId,
    TaskId,
    TaskVersionId,
    TenantId,
)
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
OTHER_COURSE = CourseId("crs_" + "9" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
KC = KcId("kc_" + "4" * 32)
PASSWORD = "correct horse battery"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.console = Console(self.database, self.store, profiles_dir=PROFILES)
        self.client = TestClient(create_app(self.console))

        with self.database.unit_of_work() as uow:
            for cid, code in ((COURSE, "prog2"), (OTHER_COURSE, "other")):
                uow.identity.save_course(
                    Course(
                        id=cid,
                        tenant_id=TENANT,
                        code=code,
                        title=f"演習 {code}",
                        term="2026",
                        subject_profile="cs_intro_c",
                    )
                )
            uow.tasks.save_task(
                Task(id=TaskId("tsk_" + "3" * 32), course_id=COURSE, title="生成課題")
            )
            uow.tasks.save_version(_version())
            uow.tasks.save_checks(
                VERSION,
                TaskChecks(
                    verification=VerificationReport(
                        reference_passes=GateOutcome.PASSED,
                        mutants_total=5,
                        mutants_killed=5,
                    ),
                    declared_kcs=("cs.loops.termination",),
                    checked_at=datetime(2026, 8, 29, tzinfo=UTC),
                ),
            )
            uow.commit()

    def register(self, login: str, role: Role, course: CourseId = COURSE):
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            service.enroll(tenant_id=TENANT, course_id=course, user_id=principal.user_id, role=role)
            uow.commit()
        return principal

    def login(self, login: str) -> None:
        response = self.client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        self.client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])


def _version() -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="## 生成された課題 ##\n\n2 つの整数を読み、和を出力しなさい。",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "5" * 32),
                code="correctness",
                title="正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
            ),
        ),
        q_matrix=(QMatrixEntry(task_version_id=VERSION, kc_id=KC),),
        max_score=100.0,
        provenance=Provenance(
            authored_by=None,
            generated_by="stub",
            generation_prompt_version="task_draft_ja@1",
            review_state=ReviewState.IN_REVIEW,
        ),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


@pytest.fixture
def world(tmp_path: Path):
    made = World(tmp_path)
    yield made
    made.database.dispose()


def _url(course: CourseId = COURSE) -> str:
    return f"/manage/courses/{course}/drafts"


def test_an_assistant_cannot_open_the_queue(world: World) -> None:
    """**TA には開けない。** 課題を出題可能にするのは教員の権限。"""
    world.register("ta1", Role.ASSISTANT)
    world.login("ta1")
    assert world.client.get(_url()).status_code == 403


def test_a_stranger_cannot_tell_the_course_exists(world: World) -> None:
    """存在と権限を区別しない（他コースを列挙させない）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    assert world.client.get(_url(OTHER_COURSE)).status_code == 404


def test_the_queue_always_shows_the_knowledge_components(world: World) -> None:
    """**機械が検証していないので、見せなければ誰も気づかない。**"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    body = world.client.get(_url()).text

    assert "cs.loops.termination" in body
    assert "課題文がこれを問うているか確認してください" in body
    assert "機械はここを検証していません" in body


def test_the_queue_says_approval_is_what_publishes(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    assert "承認するまで出題されません" in world.client.get(_url()).text


def test_approving_publishes_the_version(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    response = world.client.post(
        f"{_url()}/{VERSION}",
        data={"decision": "approve", "reason": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_version(VERSION).is_published


def test_rejecting_without_a_reason_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    response = world.client.post(
        f"{_url()}/{VERSION}",
        data={"decision": "reject", "reason": "短い"},
        follow_redirects=False,
    )
    assert response.status_code == 400

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_version(VERSION).provenance.review_state is ReviewState.IN_REVIEW


def test_rejecting_records_the_reason(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    reason = "入力の形式が課題文に書かれておらず、解答者によって読みが分かれます。"
    response = world.client.post(
        f"{_url()}/{VERSION}",
        data={"decision": "reject", "reason": reason},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        provenance = uow.tasks.get_version(VERSION).provenance
    assert provenance.review_state is ReviewState.REJECTED
    assert provenance.reject_reason == reason


def test_a_decided_version_cannot_be_decided_again(world: World) -> None:
    """やり直しは新しい版から（P8）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.login("teacher")
    world.client.post(f"{_url()}/{VERSION}", data={"decision": "approve", "reason": ""})

    again = world.client.post(
        f"{_url()}/{VERSION}",
        data={"decision": "approve", "reason": ""},
        follow_redirects=False,
    )
    assert again.status_code == 409
