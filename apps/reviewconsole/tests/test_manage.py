"""管理画面の規則を固定する。

固定したいのは権限。締切と受講の変更は成績に直接効くので、誰が何を
変更できるかが緩いと他のすべてが無意味になる。

- コースの作成 … ADMIN
- 課題・受講の管理 … そのコースの INSTRUCTOR 以上（**TA には開けない**）
- 科目プロファイル … 表示だけ。編集させない
"""

from __future__ import annotations

import io
import zipfile
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
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
        self.console = Console(
            self.database,
            FilesystemArtifactStore(tmp_path / "artifacts"),
            profiles_dir=PROFILES,
        )
        self.course, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング及び実習 2",
            term="2025-後期",
            subject_profile="cs_intro_c",
            profiles_dir=PROFILES,
        )

    def register(self, login: str, role: Role | None, course_id: CourseId | None = None):
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

    def client(self, login: str) -> TestClient:
        client = TestClient(create_app(self.console))
        response = client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])
        return client

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def _zip(root: Path, arcprefix: str = "ex9") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, f"{arcprefix}/p1/{path.relative_to(root)}")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 権限
# --------------------------------------------------------------------------


def test_an_instructor_sees_their_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get("/manage").text
    assert "プログラミング及び実習 2" in body


def test_a_learner_sees_no_courses_to_manage(world: World) -> None:
    world.register("student", Role.LEARNER)
    body = world.client("student").get("/manage").text
    assert "プログラミング及び実習 2" not in body


def test_a_learner_cannot_open_the_course_management_page(world: World) -> None:
    world.register("student", Role.LEARNER)
    response = world.client("student").get(f"/manage/courses/{world.course.id}")
    assert response.status_code == 403


def test_an_assistant_cannot_manage_the_course(world: World) -> None:
    """TA は採点を分担するが、締切と受講は変更できない。

    どちらも成績に直接効く。採点の分担と履修の管理は別の権限。
    """
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").get(f"/manage/courses/{world.course.id}")
    assert response.status_code == 403


def test_a_non_member_gets_404_not_403(world: World) -> None:
    """存在と権限を区別しない。区別するとコースを列挙できる。"""
    world.register("outsider", None)
    response = world.client("outsider").get(f"/manage/courses/{world.course.id}")
    assert response.status_code == 404


def test_only_an_admin_can_create_a_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    data = {
        "code": "network",
        "title": "ネットワーク",
        "term": "2025-後期",
        "profile": "net_python",
    }
    assert world.client("teacher").post("/manage/courses", data=data).status_code == 403


def test_an_admin_creates_a_course_and_becomes_its_instructor(world: World) -> None:
    """作った本人が担当教員にならないと、自分のコースが見えない。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    response = client.post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term": "2025-後期",
            "profile": "net_python",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    body = client.get("/manage").text
    assert "ネットワーク及び演習" in body


def test_an_unknown_subject_profile_is_refused(world: World) -> None:
    """存在しないプロファイルでコースを作ると、採点が恒久的に失敗する。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/courses",
        data={"code": "x", "title": "x", "term": "2025", "profile": "no_such_profile"},
    )
    assert response.status_code == 400
    assert "科目プロファイル" in response.json()["detail"]


# --------------------------------------------------------------------------
# 締切
# --------------------------------------------------------------------------


def _import_example(world: World) -> str:
    from aijudge_admin import import_tasks, list_tasks

    import_tasks(
        world.database,
        course_id=world.course.id,
        directory=EXAMPLE_TASK,
        profiles_dir=PROFILES,
    )
    (task, _version) = list_tasks(world.database, world.course.id)[0]
    return str(task.id)


def test_a_deadline_can_be_set(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/schedule",
        data={"opens_at": "2025-10-01T09:00", "due_at": "2025-10-08T23:59"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import TaskId

        task = uow.tasks.get_task(TaskId(task_id))
    assert task is not None
    assert task.due_at is not None
    # 締切判定がサーバのローカル時刻に依存しないこと。
    assert task.due_at.tzinfo is not None
    assert task.opens_at is not None


def test_a_deadline_before_the_opening_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/schedule",
        data={"opens_at": "2025-10-08T09:00", "due_at": "2025-10-01T09:00"},
    )
    assert response.status_code == 400


def test_a_malformed_date_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/schedule",
        data={"due_at": "来週"},
    )
    assert response.status_code == 400


def test_a_task_from_another_course_cannot_be_scheduled(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    other, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2025-後期",
        subject_profile="net_python",
        profiles_dir=PROFILES,
    )
    world.register("other_teacher", Role.INSTRUCTOR, other.id)
    task_id = _import_example(world)

    # 他コースの教員が、こちらの課題の締切を変えられないこと。
    response = world.client("other_teacher").post(
        f"/manage/courses/{other.id}/tasks/{task_id}/schedule",
        data={"due_at": "2025-10-08T23:59"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# 課題の追加（zip 取り込みは廃止した）
# --------------------------------------------------------------------------


def test_a_task_can_be_added_from_the_form(world: World) -> None:
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key": "ex02/p8",
            "statement": "## [必須] カウントアップダウン ##\n\n本文",
            "unit": "ex02",
            "session": "2",
            "position": "8",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(world.course.id)
        assert len(tasks) == 1
        version = uow.tasks.latest_version(TaskId(tasks[0].id))
    assert tasks[0].title == "カウントアップダウン"
    assert tasks[0].session == 2
    # **画面から作った課題にも AI 観点が付く。** 付かないと、その課題では
    # AI 評価器が一度も走らない（廃止した zip 取り込みがそうなっていた）。
    assert [c.code for c in version.criteria] == ["correctness", "readability"]


def test_the_form_and_the_api_produce_the_same_task(world: World) -> None:
    """経路が違っても同じものができること。分かれると片方だけ観点が欠ける。"""
    from aijudge_identity import AuthService

    principal = world.register("teacher", Role.INSTRUCTOR)
    with world.database.unit_of_work() as uow:
        _record, token = AuthService(uow.identity).issue_token(
            tenant_id=TENANT, user_id=principal.user_id, note="比較用"
        )
        uow.commit()

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key": "ex02/p1",
            "statement": "## [必須] 問 ##\n\n本文",
            "readability_weight": "0.3",
        },
    )
    api = TestClient(create_app(world.console)).post(
        f"/api/courses/{world.course.id}/tasks",
        json={"key": "ex02/p2", "statement": "## [必須] 問 ##\n\n本文", "readability_weight": 0.3},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(world.course.id)
        versions = [uow.tasks.latest_version(task.id) for task in tasks]
    assert len(versions) == 2
    assert {tuple(c.code for c in v.criteria) for v in versions} == {("correctness", "readability")}
    assert api["criteria"] == ["correctness", "readability"]


def test_a_duplicate_key_with_different_content_is_refused(world: World) -> None:
    """過去の採点基準を書き換えない（P8）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    base = {"key": "ex02/p8", "statement": "## [必須] 問 ##\n\n本文"}
    assert client.post(
        f"/manage/courses/{world.course.id}/tasks", data=base, follow_redirects=False
    ).status_code == 303

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={**base, "statement": "## [必須] 別 ##\n\n違う"},
    )
    assert response.status_code == 409


def test_a_malformed_task_key_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    for key in ("../etc/passwd", "/absolute", "ex02 p8"):
        response = client.post(
            f"/manage/courses/{world.course.id}/tasks",
            data={"key": key, "statement": "## [必須] 問 ##\n\n本文"},
        )
        assert response.status_code == 400, key


def test_an_assistant_cannot_add_a_task(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={"key": "ex02/p8", "statement": "## [必須] 問 ##\n\n本文"},
    )
    assert response.status_code == 403


def test_there_is_no_archive_upload_route(world: World) -> None:
    """zip 取り込みは廃止した。

    移行元（Sharif Judge）の形式をサーバの入口の語彙にしており、移行が
    終わったあとも一生ついて回る形だった。まとまった投入は API で行う。
    """
    app = create_app(world.console)
    for route in app.routes:
        body = getattr(getattr(route, "endpoint", None), "__code__", None)
        if body is None:
            continue
        assert "zipfile" not in (body.co_names or ()), route.path


# --------------------------------------------------------------------------
# 受講の管理
# --------------------------------------------------------------------------


def test_an_existing_user_can_be_enrolled(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("y239999", None)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "y239999", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "y239999")
        assert user is not None
        assert AuthService(uow.identity).role_in(world.course.id, user.id) is Role.LEARNER


def test_an_unknown_user_is_refused_with_a_pointer_to_the_cli(world: World) -> None:
    """新規作成はパスワードの配布が伴う。画面に平文を出さない。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "nobody", "role": "learner"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "未登録" in detail
    assert "aijudge-admin enrol" in detail


def test_a_broken_roster_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "y239999 a@b.c RANDOM[8] wizard", "role": "learner"},
    )
    assert response.status_code == 400


def test_an_enrolment_can_be_removed_without_deleting_the_user(world: World) -> None:
    """利用者を消すと過去の提出と採点の参照が壊れる。"""
    world.register("teacher", Role.INSTRUCTOR)
    student = world.register("y239999", Role.LEARNER)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/remove",
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        assert AuthService(uow.identity).role_in(world.course.id, student.user_id) is None
        assert uow.identity.get_user(student.user_id) is not None


def test_an_instructor_cannot_remove_themselves(world: World) -> None:
    """自分を外すとコースが見えなくなり、戻す手段が無い。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{teacher.user_id}/remove"
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 科目プロファイル — 表示だけ
# --------------------------------------------------------------------------


def test_the_subject_profile_is_shown_but_not_editable(world: World) -> None:
    """ブラウザから採点の設定を壊せないこと。

    評価器の指名とタイムアウトを持つ設定で、1 人の操作で全員の採点が止まる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert "cs_intro_c.yaml" in body
    assert "code_test_runner" in body, "プロファイルの内容が表示されていない"
    # 編集の口が無いこと。
    assert 'name="profile_text"' not in body
    assert "この画面から変更できません" in body


def test_there_is_no_route_that_writes_a_subject_profile(world: World) -> None:
    """将来うっかり足さないよう、経路の不在をテストで固定する。

    採点の設定を Web から書ける経路ができた瞬間、1 人の操作で全員の採点を
    止められるようになる。
    """
    app = create_app(world.console)
    # `app.routes` は include_router したものを畳まないので OpenAPI から取る。
    paths = set(app.openapi()["paths"])
    writable = {path for path in paths if "profile" in path or path.endswith("/subjects")}
    assert not writable, sorted(writable)
    # 管理画面の経路は列挙できていること（走査に失敗していない）。
    assert "/manage/courses/{course_id}" in paths


# --------------------------------------------------------------------------
# 成績の確定（ADR 0010）
# --------------------------------------------------------------------------


def test_the_grace_is_saved_on_the_course(world: World) -> None:
    """猶予はコースに持つ。科目プロファイルではない（あれは編集させない）。"""
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/auto-finalize",
        data={"after_hours": "48"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course is not None
    assert course.auto_finalize_after_hours == 48.0


def test_an_empty_grace_turns_automatic_finalization_off(world: World) -> None:
    """空欄は「自動確定しない」。既定はそれである。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(f"/manage/courses/{world.course.id}/auto-finalize", data={"after_hours": "24"})
    client.post(f"/manage/courses/{world.course.id}/auto-finalize", data={"after_hours": ""})

    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course is not None
    assert course.auto_finalize_after_hours is None


def test_a_zero_grace_is_refused(world: World) -> None:
    """0 を許すと締切と同時に確定し、締切直前の提出が採点前に確定しうる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    for value in ("0", "-1", "しばらく"):
        assert (
            client.post(
                f"/manage/courses/{world.course.id}/auto-finalize", data={"after_hours": value}
            ).status_code
            == 400
        ), value


def test_an_assistant_cannot_change_the_grace(world: World) -> None:
    """猶予は成績に直接効く。採点を分担する TA の権限とは別（既存の締切と同じ）。"""
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/auto-finalize", data={"after_hours": "24"}
    )
    assert response.status_code == 403


def test_bulk_finalization_requires_a_justification(world: World) -> None:
    """個別に読んでいない成績を確定させる操作。根拠が残らないと学習者に何も返らない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    client = world.client("teacher")

    for text in ("", "確認", "   "):
        response = client.post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/finalize",
            data={"justification": text},
        )
        assert response.status_code == 400, text


def test_bulk_finalization_refuses_a_task_from_another_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    other, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2025-後期",
        subject_profile="net_python",
        profiles_dir=PROFILES,
    )
    world.register("other_teacher", Role.INSTRUCTOR, other.id)
    task_id = _import_example(world)

    response = world.client("other_teacher").post(
        f"/manage/courses/{other.id}/tasks/{task_id}/finalize",
        data={"justification": "他コースの課題を確定しようとしています。"},
    )
    assert response.status_code == 404


def test_an_assistant_cannot_bulk_finalize(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/finalize",
        data={"justification": "TA がまとめて確定しようとしています。"},
    )
    assert response.status_code == 403


def test_a_bulk_finalization_result_does_not_leak_to_another_course(world: World) -> None:
    """`Console` は全利用者で共有なので、結果の表示にコースを添えている。

    添えないと、別コースの教員の画面に他コースの課題名が出る。
    """
    world.register("teacher", Role.INSTRUCTOR)
    other, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク",
        term="2025-後期",
        subject_profile="net_python",
        profiles_dir=PROFILES,
    )
    world.register("other_teacher", Role.INSTRUCTOR, other.id)
    task_id = _import_example(world)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/finalize",
        data={"justification": "テスト全通の提出をまとめて確定します。"},
    )

    body = world.client("other_teacher").get(f"/manage/courses/{other.id}").text
    assert "件を確定しました" not in body
