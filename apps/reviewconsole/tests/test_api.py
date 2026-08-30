"""課題を足す API の規則を固定する。

固定したいのは 4 つ。

認証     トークンでしか通らない。画面の Cookie では通らない（CSRF の経路を作らない）。
権限     コースの INSTRUCTOR 以上だけ。**TA には開けない**（画面と同じ規則）。
冪等     同じ key に同じ内容を入れ直しても増えず、締切も消えない。
不変     内容が違えば拒否する。過去の採点基準を書き換えない（P8）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import ensure_course
from aijudge_core import Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"

STATEMENT = "## [必須] カウントアップダウン ##\n\n入力された n から 1 まで降順に出力しなさい。"

SPEC = {
    "key": "ex02/p8",
    "statement": STATEMENT,
    "unit": "ex02",
    "session": 2,
    "position": 8,
    "readability_weight": 0.3,
    "test_cases": [
        {"name": "1", "input": "3\n", "expected": "3 2 1\n"},
        {"name": "2", "input": "1\n", "expected": "1\n"},
    ],
}


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
        self.console = Console(
            self.database,
            FilesystemArtifactStore(tmp_path / "artifacts"),
            profiles_dir=PROFILES,
        )
        self.client = TestClient(create_app(self.console))
        self.course, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング及び実習 2",
            term="2025-後期",
            subject_profile="cs_intro_c",
            profiles_dir=PROFILES,
        )

    def user(self, login: str, role: Role | None, course_id: CourseId | None = None):
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            if role is not None:
                service.enroll(
                    tenant_id=TENANT,
                    course_id=course_id or self.course.id,
                    user_id=principal.user_id,
                    role=role,
                )
            uow.commit()
        return principal

    def token(self, login: str, role: Role | None = Role.INSTRUCTOR) -> str:
        principal = self.user(login, role)
        with self.database.unit_of_work() as uow:
            _record, token = AuthService(uow.identity).issue_token(
                tenant_id=TENANT, user_id=principal.user_id, note="テスト用の流し込み"
            )
            uow.commit()
        return token

    def auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 認証
# --------------------------------------------------------------------------


def test_a_token_is_required(world: World) -> None:
    response = world.client.post(f"/api/courses/{world.course.id}/tasks", json=SPEC)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_a_bogus_token_is_refused(world: World) -> None:
    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth("aij_nope")
    )
    assert response.status_code == 401


def test_a_session_cookie_does_not_open_the_api(world: World) -> None:
    """**画面の Cookie では通さない。**

    通すと、教員がログインしたまま別サイトを開いたときに、そのサイトから
    課題を書き換えられる（CSRF）。API は Cookie を見ないので経路が無い。
    """
    world.user("teacher", Role.INSTRUCTOR)
    login = world.client.post(
        "/login", data={"login": "teacher", "password": PASSWORD}, follow_redirects=False
    )
    world.client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])

    response = world.client.post(f"/api/courses/{world.course.id}/tasks", json=SPEC)
    assert response.status_code == 401


def test_a_revoked_token_stops_working(world: World) -> None:
    principal = world.user("teacher", Role.INSTRUCTOR)
    with world.database.unit_of_work() as uow:
        record, token = AuthService(uow.identity).issue_token(
            tenant_id=TENANT, user_id=principal.user_id, note="テスト用の流し込み"
        )
        uow.commit()
    assert world.client.get("/api/whoami", headers=world.auth(token)).status_code == 200

    with world.database.unit_of_work() as uow:
        AuthService(uow.identity).revoke_token(record.id)
        uow.commit()

    assert world.client.get("/api/whoami", headers=world.auth(token)).status_code == 401


def test_whoami_names_the_user_the_token_acts_as(world: World) -> None:
    token = world.token("teacher")
    body = world.client.get("/api/whoami", headers=world.auth(token)).json()
    assert body["login"] == "teacher"


# --------------------------------------------------------------------------
# 権限
# --------------------------------------------------------------------------


def test_an_assistant_cannot_add_a_task(world: World) -> None:
    """TA には開けない。課題と締切の管理は採点の分担とは別の権限（画面と同じ）。"""
    token = world.token("ta", Role.ASSISTANT)
    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    )
    assert response.status_code == 403


def test_a_non_member_gets_404_not_403(world: World) -> None:
    """存在と権限を区別しない。区別すると、コース ID を総当たりで集められる。"""
    token = world.token("outsider", None)
    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    )
    assert response.status_code == 404


def test_the_course_listing_hides_what_the_token_cannot_touch(world: World) -> None:
    token = world.token("ta", Role.ASSISTANT)
    assert world.client.get("/api/courses", headers=world.auth(token)).json() == []


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------


def test_a_task_is_created_with_both_criteria(world: World) -> None:
    token = world.token("teacher")
    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["title"] == "カウントアップダウン"
    assert body["test_cases"] == 2
    assert body["auto_graded"] is True
    # readability_weight を渡したので AI 観点が付く。ここが 1 つだけなら、
    # 実運用で AI 評価器が一度も走らない（それが zip 取り込みで起きていた）。
    assert body["criteria"] == ["correctness", "readability"]


def test_posting_the_same_task_twice_changes_nothing(world: World) -> None:
    """移行では同じディレクトリを何度も流す。二度目で増えては使えない。"""
    token = world.token("teacher")
    first = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    ).json()
    second = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    ).json()

    assert second["created"] is False
    assert second["task_id"] == first["task_id"]
    assert (
        len(
            world.client.get(
                f"/api/courses/{world.course.id}/tasks", headers=world.auth(token)
            ).json()
        )
        == 1
    )


def test_changing_the_statement_is_refused(world: World) -> None:
    """過去の採点基準を書き換えない（P8）。直すなら版を上げる操作が要る。"""
    token = world.token("teacher")
    world.client.post(f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token))
    changed = {**SPEC, "statement": "## [必須] 別の問題 ##\n\n違う内容"}

    response = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=changed, headers=world.auth(token)
    )
    assert response.status_code == 409


def test_reimporting_does_not_wipe_a_deadline_set_in_the_ui(world: World) -> None:
    """**流し込みの再実行が締切を消さない。** 消すと成績の期限が飛ぶ。"""
    from datetime import UTC, datetime

    from aijudge_core.ids import TaskId

    token = world.token("teacher")
    created = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token)
    ).json()

    due = datetime(2026, 10, 8, 23, 59, tzinfo=UTC)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(created["task_id"]))
        uow.tasks.save_task(task.model_copy(update={"due_at": due}))
        uow.commit()

    world.client.post(f"/api/courses/{world.course.id}/tasks", json=SPEC, headers=world.auth(token))

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(created["task_id"]))
    assert task.due_at == due


def test_a_task_without_test_cases_is_graded_by_the_ai_criterion(world: World) -> None:
    """自動テストがまだ無い課題は実在する（サーバ課題・自己採点・レポート）。

    決定的評価器に担当させると、その観点は永久に採点されず全提出が
    教員に積まれる。
    """
    token = world.token("teacher")
    spec = {**SPEC, "key": "ex11/p1", "test_cases": []}

    body = world.client.post(
        f"/api/courses/{world.course.id}/tasks", json=spec, headers=world.auth(token)
    ).json()

    assert body["auto_graded"] is False
    assert body["test_cases"] == 0


def test_a_malformed_key_is_refused(world: World) -> None:
    token = world.token("teacher")
    for key in ("../etc/passwd", "/absolute", "ex02 p8", ""):
        response = world.client.post(
            f"/api/courses/{world.course.id}/tasks",
            json={**SPEC, "key": key},
            headers=world.auth(token),
        )
        assert response.status_code == 422, key


def test_the_listing_shows_what_was_loaded(world: World) -> None:
    token = world.token("teacher")
    for position in (1, 2, 3):
        world.client.post(
            f"/api/courses/{world.course.id}/tasks",
            json={**SPEC, "key": f"ex02/p{position}", "position": position},
            headers=world.auth(token),
        )

    rows = world.client.get(
        f"/api/courses/{world.course.id}/tasks", headers=world.auth(token)
    ).json()
    assert [row["position"] for row in rows] == [1, 2, 3]
