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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import ensure_course
from aijudge_authoring.statement import render_statement
from aijudge_core import Role
from aijudge_core.ids import CourseId, TaskVersionId, TenantId
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

    def register(
        self,
        login: str,
        role: Role | None,
        course_id: CourseId | None = None,
        *,
        tenant_admin: bool = False,
    ):
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
            if tenant_admin:
                # #128: コースの受講とは別の、テナント全体の管理者フラグ。
                service.set_tenant_admin(principal.user_id, admin=True)
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


def test_a_learner_sees_their_course_but_nothing_to_manage(world: World) -> None:
    """**役割はコースごとに決まる**（#103）。学習者にもコースは出す ── 出さないと
    入口が 2 つあること自体に気づけない。ただし採点の行としては出さない。
    """
    world.register("student", Role.LEARNER)
    body = world.client("student").get("/manage").text
    assert "プログラミング及び実習 2" in body
    assert "受講しているコース" in body
    # 採点の入口は出ない（担当していないので）。
    assert "採点を担当しているコースがありません" in body
    assert f"/courses/{world.course.id}/queue" not in body


def test_a_user_with_no_enrolments_is_told_so(world: World) -> None:
    """**空でも見出しは消さない**（#131）。SSO 直後の利用者はどのコースにも
    受講登録が無いのが普通に起きる ── 見出しごと消すと、ログインできたのに
    画面に何も無いように見える。
    """
    world.register("nobody", None)
    body = world.client("nobody").get("/manage").text
    assert "受講しているコース" in body
    assert "受講しているコースがありません" in body
    assert "採点を担当しているコースがありません" in body


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
        "instructors": "teacher",
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
            "instructors": "boss",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    body = client.get("/manage").text
    assert "ネットワーク及び演習" in body


def test_creating_a_course_requires_at_least_one_instructor(world: World) -> None:
    """#130: 担当教員を指定しないコースは作れない。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term": "2025-後期",
            "profile": "net_python",
            "instructors": "   \n  ",
        },
    )
    assert response.status_code == 400


def test_creating_a_course_with_an_unregistered_instructor_is_refused(world: World) -> None:
    """#130: 新規利用者はここでは作れない（`add_enrolments` と同じ規則）。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term": "2025-後期",
            "profile": "net_python",
            "instructors": "nobody-yet",
        },
    )
    assert response.status_code == 400
    assert "nobody-yet" in response.json()["detail"]


def test_creating_a_course_enrolls_the_specified_instructor(world: World) -> None:
    """#130: 作成者以外を指定すれば、その利用者も担当教員になる。"""
    boss = world.register("boss", Role.ADMIN)
    other_teacher = world.register("other-teacher", None)
    client = world.client("boss")
    response = client.post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term": "2025-後期",
            "profile": "net_python",
            "instructors": "other-teacher",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    course_id = CourseId(response.headers["location"].split("/")[-1])
    with world.database.unit_of_work() as uow:
        other_enrollment = uow.identity.find_enrollment(course_id, other_teacher.user_id)
        # 作成者自身も引き続き担当教員になる（#128 が入るまでの措置）。
        boss_enrollment = uow.identity.find_enrollment(course_id, boss.user_id)
    assert other_enrollment is not None and other_enrollment.role is Role.INSTRUCTOR
    assert boss_enrollment is not None and boss_enrollment.role is Role.INSTRUCTOR


def test_a_tenant_admin_manages_a_course_they_never_enrolled_in(world: World) -> None:
    """#128: 管理者は受講登録なしでどのコースの教員権限も持つ。

    `world.course` は fixture が作った既存のコースで、`boss` はどの受講にも
    登録していない ── それでも設定画面を開けて、`can_manage` の起点も
    `is_tenant_admin` だけで足りることを確かめる。
    """
    boss = world.register("boss", None, tenant_admin=True)
    with world.database.unit_of_work() as uow:
        assert uow.identity.find_enrollment(world.course.id, boss.user_id) is None

    client = world.client("boss")
    response = client.get(f"/manage/courses/{world.course.id}")
    assert response.status_code == 200
    assert "プログラミング及び実習 2" in response.text


def test_a_tenant_admin_sees_every_course_on_the_landing_page(world: World) -> None:
    """`courses_for` が全コースを返すので（#128）、担当コースの一覧に
    受講登録の無いコースも並ぶ。
    """
    world.register("boss", None, tenant_admin=True)
    body = world.client("boss").get("/manage").text
    assert "プログラミング及び実習 2" in body
    # 「担当しているコースがありません」ではなく、実際の一覧が出ている。
    assert "採点を担当しているコースがありません" not in body


# --------------------------------------------------------------------------
# 利用者の作成（画面から、管理者専用 — #127）
# --------------------------------------------------------------------------


def test_only_an_admin_can_open_the_new_user_form(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    assert world.client("teacher").get("/manage/users/new").status_code == 403


def test_only_an_admin_can_create_a_user(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        "/manage/users", data={"login": "newperson", "display_name": ""}
    )
    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        assert uow.identity.find_user_by_login(TENANT, "newperson") is None


def test_an_admin_creates_a_local_user_and_sees_the_password_once(world: World) -> None:
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/users", data={"login": "newperson", "display_name": "新しい人"}
    )
    assert response.status_code == 200
    assert "newperson" in response.text
    # パスワードが生成され、この応答にだけ表示される。
    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "newperson")
    assert user is not None
    assert not user.is_tenant_admin


def test_an_admin_can_create_another_tenant_admin(world: World) -> None:
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/users",
        data={"login": "boss2", "display_name": "", "tenant_admin": "true"},
    )
    assert response.status_code == 200
    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "boss2")
    assert user is not None and user.is_tenant_admin


def test_creating_a_duplicate_login_is_refused(world: World) -> None:
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/users", data={"login": "boss", "display_name": ""}
    )
    assert response.status_code == 400


def test_an_unknown_subject_profile_is_refused(world: World) -> None:
    """存在しないプロファイルでコースを作ると、採点が恒久的に失敗する。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/courses",
        data={
            "code": "x",
            "title": "x",
            "term": "2025",
            "profile": "no_such_profile",
            "instructors": "boss",
        },
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


def _unit_of(world: World) -> str:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    return task.unit or "_"


def test_the_schedule_is_set_for_the_whole_problem_set(world: World) -> None:
    """**日程は問題セットで揃える。** 課題ごとに違う締切を持てると、

    同じセットの中で締切がずれ、「この回はいつまでか」が言えなくなる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={
            "opens_at": "2025-10-01T09:00",
            "submissions_open_at": "2025-10-01T13:00",
            "due_at": "2025-10-08T23:59",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    # 猶予と回番号は別のフォーム。効き方が違うものを 1 つの保存に混ぜない。
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/auto-finalize",
        data={"after_minutes": "90"},
    )
    client.post(f"/manage/courses/{world.course.id}/units/{unit}/number", data={"session": "3"})

    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(world.course.id)
    assert tasks
    for task in tasks:
        assert task.opens_at is not None
        assert task.submissions_open_at is not None
        assert task.due_at is not None
        # 締切判定がサーバのローカル時刻に依存しないこと。
        assert task.due_at.tzinfo is not None
        assert task.auto_finalize_after_minutes == 90
        assert task.session == 3


def test_a_deadline_before_the_opening_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/schedule",
        data={"opens_at": "2025-10-08T09:00", "due_at": "2025-10-01T09:00"},
    )
    assert response.status_code == 400


def test_a_deadline_before_the_submissions_open_is_refused(world: World) -> None:
    """提出開始より前に締め切る課題は、誰も提出できないまま締切を迎える。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/schedule",
        data={"submissions_open_at": "2025-10-08T09:00", "due_at": "2025-10-01T09:00"},
    )
    assert response.status_code == 400


def test_a_malformed_date_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/schedule",
        data={"due_at": "来週"},
    )
    assert response.status_code == 400


def test_a_problem_set_from_another_course_cannot_be_scheduled(world: World) -> None:
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
    _import_example(world)
    unit = _unit_of(world)

    # 他コースの教員が、こちらの問題セットの締切を変えられないこと。
    response = world.client("other_teacher").post(
        f"/manage/courses/{other.id}/units/{unit}/schedule",
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
    # 回番号は問題セットから引き継ぐ。最初の 1 問なので空のまま。
    assert tasks[0].session is None
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
    api = (
        TestClient(create_app(world.console))
        .post(
            f"/api/courses/{world.course.id}/tasks",
            json={
                "key": "ex02/p2",
                "statement": "## [必須] 問 ##\n\n本文",
                "readability_weight": 0.3,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        .json()
    )

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
    assert (
        client.post(
            f"/manage/courses/{world.course.id}/tasks", data=base, follow_redirects=False
        ).status_code
        == 303
    )

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


def test_the_template_itself_stays_read_only(world: World) -> None:
    """雛形は共有の既定。**ブラウザからは変えられない。**

    同じ雛形を使う他のコースにも効くので、1 人の操作で全員の採点が止まる。
    コースごとの調整は上書きで行う（`aijudge_grading.overrides`）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    # 実効設定は見える（評価器の名前が並ぶ）。
    assert "code_test_runner" in body
    # 雛形そのものを書き換える口は無い。
    assert 'name="profile_text"' not in body
    assert "ここからは変えません" in body


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
        data={"after_minutes": "2880"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course is not None
    assert course.auto_finalize_after_minutes == 2880


def test_an_empty_grace_turns_automatic_finalization_off(world: World) -> None:
    """空欄は「自動確定しない」。既定はそれである。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(f"/manage/courses/{world.course.id}/auto-finalize", data={"after_minutes": "1440"})
    client.post(f"/manage/courses/{world.course.id}/auto-finalize", data={"after_minutes": ""})

    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course is not None
    assert course.auto_finalize_after_minutes is None


def test_a_zero_grace_is_refused(world: World) -> None:
    """0 を許すと締切と同時に確定し、締切直前の提出が採点前に確定しうる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    for value in ("0", "-1", "しばらく"):
        assert (
            client.post(
                f"/manage/courses/{world.course.id}/auto-finalize", data={"after_minutes": value}
            ).status_code
            == 400
        ), value


def test_an_assistant_cannot_change_the_grace(world: World) -> None:
    """猶予は成績に直接効く。採点を分担する TA の権限とは別（既存の締切と同じ）。"""
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/auto-finalize", data={"after_minutes": "1440"}
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

    # 結果は回ごとのページに出る。別コースの回を開いて漏れていないことを見る。
    body = world.client("other_teacher").get(f"/manage/courses/{other.id}/units/_").text
    assert "件を確定しました" not in body


# --------------------------------------------------------------------------
# 画面の構成 — 担当コース → 科目のメニュー → 回
# --------------------------------------------------------------------------


def test_the_course_list_shows_a_digest(world: World) -> None:
    """コード・コース名・学期だけでは、どのコースに用があるか開くまで分からない。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    body = world.client("teacher").get("/").text
    assert "問題セット" in body
    assert "課題" in body
    assert "未確定" in body
    assert "異議" in body
    assert "未承認" in body


def test_the_management_index_is_folded_into_the_course_list(world: World) -> None:
    """コースの一覧は 1 つだけ。入口が 2 つあること自体が分かりにくさの元だった。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").get("/manage", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_course_page_is_a_menu(world: World) -> None:
    """科目ページは分岐だけを持つ。設定を 1 枚に積むと目で探すことになる。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    body = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert "コース全体の設定" in body
    assert f"/manage/courses/{world.course.id}" in body
    assert f"/courses/{world.course.id}/queue" in body
    assert "再確認の依頼" in body
    # 課題そのものの操作（締切の入力欄）はここには無い。
    assert 'name="due_at"' not in body


def test_the_course_settings_page_no_longer_carries_the_tasks(world: World) -> None:
    """課題は回ごとのページへ移した。科目全体の設定と混ぜない。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "成績の自動確定" in body
    # 採点設定は同じページに置く（別ページに分けない）。
    assert "採点設定" in body
    assert 'name="statement"' not in body, "課題の追加フォームが残っている"


def test_a_unit_page_carries_only_its_own_tasks(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")

    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    key = task.unit or "_"

    body = client.get(f"/manage/courses/{world.course.id}/units/{key}").text
    assert task.title in body
    assert 'name="due_at"' in body, "締切を設定できない"
    assert 'name="submissions_open_at"' in body, "提出開始を設定できない"
    # 課題の追加と訂正は課題のページで行う（一覧からたどる）。
    assert f"/units/{key}/tasks/new" in body

    # 別の問題セットには出てこない。
    other = client.get(f"/manage/courses/{world.course.id}/units/nosuchunit").text
    assert task.title not in other


def test_an_unknown_unit_opens_as_an_empty_one(world: World) -> None:
    """回は課題が持つ属性で、それ自体の記録は無い。

    知らない鍵を 404 にすると、新しい回に最初の 1 問を足す導線が無くなる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/ex04")
    assert response.status_code == 200
    assert "課題がありません" in response.text


def test_opening_a_new_unit_redirects_to_its_page(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units",
        data={"unit": "ex04"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/manage/courses/{world.course.id}/units/ex04"


# --------------------------------------------------------------------------
# 役割ごとの境界（#102）。**4 つの役割を同じ場所で突き合わせる。**
#
# 権限は 1 か所ずつ書かれているので、個別に見ると正しくても、並べると
# 「TA には出さないと書いたものが admin にも出ない」のような穴が残る。
# --------------------------------------------------------------------------


def _world_with_every_role(world: World):
    """learner / assistant / instructor / admin を 1 人ずつ揃える。"""
    world.register("s2400001", Role.LEARNER)
    world.register("ta", Role.ASSISTANT)
    world.register("teacher", Role.INSTRUCTOR)
    world.register("chief", Role.ADMIN)
    _import_example(world)
    return _unit_of(world)


def test_a_learner_reaches_nothing_under_manage(world: World) -> None:
    """**learner は /manage に入れない。** 1 件しか確かめていなかった。"""
    unit = _world_with_every_role(world)
    client = world.client("s2400001")
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    for path in (
        f"/manage/courses/{world.course.id}",
        f"/manage/courses/{world.course.id}/units/{unit}",
        f"/manage/courses/{world.course.id}/tasks/{task.id}/edit",
        f"/manage/courses/{world.course.id}/enrolments",
        f"/manage/courses/{world.course.id}/drafts",
        f"/manage/courses/{world.course.id}/kc",
    ):
        assert client.get(path).status_code in (403, 404), f"{path} が learner に開いた"

    # コンソール側では「受講しているコース」として出る（#103）。採点の行では
    # ないので、そこから採点の画面へは行けない。
    landing = client.get("/").text
    assert world.course.title in landing
    assert "受講しているコース" in landing
    assert f"/courses/{world.course.id}/queue" not in landing


def test_an_assistant_reads_a_task_but_gets_no_editor(world: World) -> None:
    """TA は課題を読める。**公開前でも読める** ── 採点の用意は公開前に始まる。"""
    _world_with_every_role(world)
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    page = world.client("ta").get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit")
    assert page.status_code == 200
    assert "読むだけの画面です" in page.text
    # 問題文は**学習者と同じ描画**で出す（Markdown のままにしない）。
    assert "<textarea" not in page.text, "訂正の欄が出ている"
    assert "revise" not in page.text, "訂正の送り先が出ている"


def test_an_assistant_cannot_change_anything(world: World) -> None:
    """読めることと直せることを取り違えない。**書き込みは全部 403。**"""
    unit = _world_with_every_role(world)
    client = world.client("ta")
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    writes = (
        (
            f"/manage/courses/{world.course.id}/units/{unit}/schedule",
            {"due_at": "2026-10-08T23:59"},
        ),
        (f"/manage/courses/{world.course.id}/tasks/{task.id}/revise", {"statement": "## x ##"}),
        (
            f"/manage/courses/{world.course.id}/tasks/{task.id}/finalize",
            {"justification": "x" * 40},
        ),
        (f"/manage/courses/{world.course.id}/tasks/{task.id}/withdraw", {}),
        (
            f"/manage/courses/{world.course.id}/enrolments",
            {"roster": "s2400001", "role": "learner"},
        ),
        (f"/manage/courses/{world.course.id}/upload-formats", {"suffix": [".c"]}),
    )
    for path, data in writes:
        assert client.post(path, data=data).status_code == 403, f"{path} が TA に通った"


def test_the_course_menu_shows_each_role_what_it_can_use(world: World) -> None:
    """**押すと 403 になるリンクを出さない。** 作問は以前 TA にも見えていた。"""
    _world_with_every_role(world)

    ta_page = world.client("ta").get(f"/courses/{world.course.id}").text
    assert "問題セット" in ta_page, "TA に問題セットが出ていない"
    assert "作問" not in ta_page, "TA に作問が出ている"
    assert "受講者" not in ta_page or "/enrolments" not in ta_page

    teacher_page = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert "作問" in teacher_page
    assert "/enrolments" in teacher_page


def test_an_admin_gets_everything_an_instructor_gets(world: World) -> None:
    """**admin は全部できる。** 役割を足すたびに admin を確かめ直さないと、
    「教員以上」と書いたつもりの門が教員だけになる。
    """
    unit = _world_with_every_role(world)
    client = world.client("chief")
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    for path in (
        f"/manage/courses/{world.course.id}",
        f"/manage/courses/{world.course.id}/units/{unit}",
        f"/manage/courses/{world.course.id}/tasks/{task.id}/edit",
        f"/manage/courses/{world.course.id}/enrolments",
        f"/manage/courses/{world.course.id}/drafts",
    ):
        assert client.get(path).status_code == 200, f"{path} が admin に開かない"

    # 読むだけの画面ではない（編集の欄がある）。
    editor = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "<textarea" in editor and "読むだけの画面です" not in editor

    # 書き込みも通る。日程を入れて、入ったことを確かめる。
    assert (
        client.post(
            f"/manage/courses/{world.course.id}/units/{unit}/schedule",
            data={"due_at": "2026-10-08T23:59"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with world.database.unit_of_work() as uow:
        (updated,) = uow.tasks.list_for_course(world.course.id)
    assert updated.due_at is not None


def test_bulk_finalisation_stays_with_the_instructor(world: World) -> None:
    """**まとめての確定は読んでいない成績を閉じる操作**。TA には出さない。"""
    unit = _world_with_every_role(world)
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    reason = "テスト実行の結果を確認したうえで、残りをまとめて確定します。" * 2
    assert (
        world.client("ta")
        .post(
            f"/manage/courses/{world.course.id}/tasks/{task.id}/finalize",
            data={"justification": reason},
        )
        .status_code
        == 403
    )
    assert (
        world.client("ta")
        .post(
            f"/manage/courses/{world.course.id}/units/{unit}/finalize",
            data={"justification": reason},
        )
        .status_code
        == 403
    )
    # 画面にも出さない（押せないボタンを見せない）。
    page = world.client("ta").get(f"/courses/{world.course.id}/finalize").text
    assert "まとめて" not in page or "/finalize" not in page.split("まとめて")[1][:400]


def test_an_assistant_reads_a_unit_page_without_the_forms(world: World) -> None:
    """**読むことと直すことは別の権限**（#102）。

    以前はここが 403 だった。TA が課題を読めないと、学習者の質問にも自分が
    採点している提出にも答えられない。日程と一括確定は成績に直接効くので、
    出さない ── 隠すのではなく、読むだけの画面を別に出す。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    _import_example(world)
    unit = _unit_of(world)

    page = world.client("ta").get(f"/manage/courses/{world.course.id}/units/{unit}")
    assert page.status_code == 200
    assert "読むだけの画面です" in page.text
    # **成績に効く操作は無い。** 隠されているのではなく、出ていない。
    for gone in ("/schedule", "/finalize", "/clear", "/tasks/new"):
        assert gone not in page.text, f"TA の画面に {gone} が出ている"
    # ログアウト以外に送り先の無い画面である（`/manage/...` を叩く欄が無い）。
    assert 'action="/manage/' not in page.text, "TA の画面に設定を送る欄がある"


def test_setting_the_schedule_returns_to_the_problem_set(world: World) -> None:
    """日程を触る操作は、その問題セットのページから来てそこへ戻る。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"due_at": "2025-10-08T23:59"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        f"/manage/courses/{world.course.id}/units/{unit}"
    )


def test_a_new_task_inherits_the_schedule_of_its_problem_set(world: World) -> None:
    """日程はセットの性質。追加した課題だけ締切が無い、が起きないこと。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"opens_at": "2025-10-01T09:00", "due_at": "2025-10-08T23:59"},
    )
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/auto-finalize",
        data={"after_minutes": "90"},
    )
    client.post(f"/manage/courses/{world.course.id}/units/{unit}/number", data={"session": "3"})

    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p9",
            "unit": unit,
            "statement": "## [必須] 追加した課題 ##\n\n本文",
            "position": "9",
            "readability_weight": "0.3",
        },
    )

    with world.database.unit_of_work() as uow:
        added = next(
            task
            for task in uow.tasks.list_for_course(world.course.id)
            if task.title == "追加した課題"
        )
    assert added.due_at is not None
    assert added.auto_finalize_after_minutes == 90
    assert added.session == 3


# --------------------------------------------------------------------------
# 課題キーの前半は問題セットが決める
# --------------------------------------------------------------------------


def test_the_unit_fixes_the_first_half_of_the_task_key(world: World) -> None:
    """鍵は同一性そのもの。打ち間違えたぶんは別の課題として増える。

    回のページから追加する限り `ex04/` は動かず、教員が打つのは `p1` だけ。
    """
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## 課題 ##\n\n本文",
            "session": "4",
            "position": "1",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    # 保存すると、その課題のページに残る（続けて観点を直せる）。
    assert "/edit" in response.headers["location"]

    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.unit == "ex04"


def test_a_suffix_that_already_carries_the_prefix_is_not_doubled(world: World) -> None:
    """取り込み済みの課題と鍵を揃えたい場合に、前半を二重に付けない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    body = {
        "unit": "ex04",
        "statement": "## 課題 ##\n\n本文",
        "session": "4",
        "position": "1",
        "readability_weight": "0.3",
    }
    assert (
        client.post(
            f"/manage/courses/{world.course.id}/tasks",
            data={**body, "key_suffix": "ex04/p1"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    # 同じ鍵なので、二度目は増えずに同じ課題を更新する（`derived_id`）。
    assert (
        client.post(
            f"/manage/courses/{world.course.id}/tasks",
            data={**body, "key_suffix": "p1"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with world.database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(world.course.id)
    assert len(tasks) == 1, "前半が二重に付いて別の課題になっている"


def test_a_task_without_any_key_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={"unit": "ex04", "statement": "## 課題 ##\n\n本文"},
    )
    assert response.status_code == 400
    assert "課題キー" in response.json()["detail"]


def test_the_unit_page_does_not_let_you_retype_the_unit(world: World) -> None:
    """「まとまり」の自由入力は置かない。別の回の課題をここから作れてしまう。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/units/ex04/tasks/new").text
    )
    assert 'name="key_suffix"' in body
    assert 'name="unit" value="ex04"' in body
    assert 'id="unit"' not in body, "まとまりの自由入力が残っている"


def test_the_course_menu_puts_the_units_first(world: World) -> None:
    """よく使うのは「いまの回」。設定と同じ濃さで並べると毎回目で探すことになる。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    body = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert body.index("問題セット") < body.index("コース全体の設定")


# --------------------------------------------------------------------------
# 提出できるファイル形式
# --------------------------------------------------------------------------


def test_the_course_declares_a_default_upload_format(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/upload-formats",
        data={"suffix": [".c", ".pdf", ".png"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.upload_suffixes == (".c", ".pdf", ".png")


def test_an_empty_format_selection_is_refused(world: World) -> None:
    """1 つも選ばせないと、その科目には何も提出できなくなる。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/upload-formats", data={}
    )
    assert response.status_code == 400


def test_a_task_can_declare_its_own_upload_formats(world: World) -> None:
    """レポート 1 問だけ PDF を許す、が課題側でできること。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] レポート ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
            "suffix": [".pdf", ".jpg"],
        },
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.accepted_suffixes == (".jpg", ".pdf")


# --------------------------------------------------------------------------
# 既にある課題を直す（P8 — 版を上げる）
# --------------------------------------------------------------------------


def test_revising_a_task_creates_a_new_version(world: World) -> None:
    """出題済みの版は書き換えない。過去の採点がどの基準で付いたか辿れなくなる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 元の題名 ##\n\n元の本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 元の題名 ##\n\n直した本文",
            "readability_weight": "0.3",
            "suffix": [".pdf"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
        again = uow.tasks.get_version(first.id)
        updated = uow.tasks.get_task(task.id)
    assert latest.version == first.version + 1
    assert "直した本文" in latest.statement
    # 元の版はそのまま残る。
    assert again is not None
    assert "元の本文" in again.statement
    assert updated.accepted_suffixes == (".pdf",)


def test_revising_without_changing_anything_does_not_bump_the_version(world: World) -> None:
    """提出形式だけ変えたいときに版が増えないこと。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    statement = "## [必須] 題名 ##\n\n本文"
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": statement,
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={"statement": statement, "readability_weight": "0.3", "suffix": [".png"]},
    )
    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
        updated = uow.tasks.get_task(task.id)
    assert latest.version == 1
    assert updated.accepted_suffixes == (".png",)


def test_an_assistant_cannot_revise_a_task(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## 題名 ##\n\n本文"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# 確定処理 — 提出ごと・問題ごと・問題セットごと
# --------------------------------------------------------------------------


def test_the_finalization_page_lists_the_open_submissions(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    body = world.client("teacher").get(f"/courses/{world.course.id}/finalize").text
    assert "確定処理" in body
    assert "提出ごとに確定する" in body
    assert "問題セットごとにまとめて確定する" in body


def test_a_problem_set_can_be_finalized_in_one_go(world: World) -> None:
    """問題セット単位の一括確定。学期末に成績を閉じる導線（ADR 0010）。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/finalize",
        data={"justification": "テスト全通の提出について、抽出して確認の上まとめて確定します。"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/courses/{world.course.id}/finalize"


def test_finalizing_a_problem_set_requires_a_justification(world: World) -> None:
    """根拠は学習者にそのまま出る。個別に読んでいない成績を閉じる操作だから。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/finalize",
        data={"justification": "短い"},
    )
    assert response.status_code == 400


def test_an_assistant_cannot_finalize_a_problem_set(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    _import_example(world)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/finalize",
        data={"justification": "テスト全通の提出をまとめて確定します。"},
    )
    assert response.status_code == 403


def test_an_empty_problem_set_cannot_be_finalized(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/nosuchunit/finalize",
        data={"justification": "テスト全通の提出をまとめて確定します。"},
    )
    assert response.status_code == 404


def test_the_grace_and_the_number_are_separate_forms(world: World) -> None:
    """効き方が違うものを 1 つの保存ボタンに混ぜない。

    締切を直しに来たときに猶予まで書き換える（あるいはその逆）事故を、
    フォームの単位で防ぐ。
    """
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/auto-finalize",
        data={"after_minutes": "45"},
    )
    client.post(f"/manage/courses/{world.course.id}/units/{unit}/number", data={"session": "7"})

    # 日程だけを保存しても、猶予と回番号は残る。
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"due_at": "2025-10-08T23:59"},
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.auto_finalize_after_minutes == 45
    assert task.session == 7
    assert task.due_at is not None


def test_an_empty_grace_falls_back_to_the_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/auto-finalize",
        data={"after_minutes": "45"},
    )
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/auto-finalize", data={"after_minutes": ""}
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.auto_finalize_after_minutes is None


def test_a_new_task_carries_the_course_default_formats(world: World) -> None:
    """**空で保存しない。** 画面は科目の既定をチェック済みで出す。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/upload-formats", data={"suffix": [".py", ".md"]}
    )
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 課題 ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.accepted_suffixes == (".md", ".py")


def test_clearing_every_format_on_the_form_is_refused(world: World) -> None:
    """空で保存できると、その課題には何も提出できなくなる。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 課題 ##\n\n本文",
            "readability_weight": "0.3",
            # 画面から来たことの印。チェックは 1 つも無い。
            "formats": "1",
        },
    )
    assert response.status_code == 400
    assert "1 つ以上" in response.json()["detail"]


def test_saving_the_schedule_says_so_on_the_next_screen(world: World) -> None:
    """同じ画面に戻る操作は、成功しても見た目が変わらない。

    押せていないのか効いていないのかを教員が区別できないので、合図を出す。
    """
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"due_at": "2025-10-08T23:59"},
        follow_redirects=False,
    )
    # **その場に戻す。** 錨が付いていないと、保存のたびに画面の先頭に飛ぶ。
    assert response.headers["location"].endswith("?saved=schedule#schedule")
    assert "日程を保存しました" in client.get(response.headers["location"]).text


def test_saving_the_course_settings_says_so(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/auto-finalize",
        data={"after_minutes": "60"},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("?saved=course_grace#course_grace")
    assert "保存しました" in client.get(response.headers["location"]).text


# --------------------------------------------------------------------------
# 知識要素の体系（設計原則 P6）
# --------------------------------------------------------------------------


def test_the_kc_page_shows_the_namespaces_of_the_course(world: World) -> None:
    """**共有されるのは語彙で、見えるかどうかはコースごとの宣言が決める。**

    以前は「他のコースにも見えます」と書いていたが、#37 で範囲を絞っている
    コースの一覧からは外したので、そのままでは嘘になっていた。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "知識要素" in body
    assert "cs" in body
    assert "コースには属しません" in body
    assert "そのコースで選ぶまで一覧には出ません" in body


def test_an_instructor_cannot_create_a_root_component(world: World) -> None:
    """新しい分野の根を作るのは管理者の操作。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.loops", "label": "ループ"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "第 1 階層" in detail
    # **名前空間が階層に数えられないことを言う。** `cs.loops` は点で 2 つに
    # 分かれて見えるので、「第 1 階層」だけでは何を指すのか読めない。
    assert "名前空間" in detail and "階層に数えません" in detail
    assert "cs.loops.…" in detail


def test_an_admin_creates_a_root_and_an_instructor_extends_it(world: World) -> None:
    """禁止ではなく、追加を明示的な行為にする。"""
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    assert (
        world.client("boss")
        .post(
            f"/manage/courses/{world.course.id}/kc",
            data={"key": "cs.loops", "label": "ループ"},
            follow_redirects=False,
        )
        .status_code
        == 303
    )
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.loops.termination", "label": "停止条件"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "cs.loops.termination" in body


def test_an_instructor_can_correct_a_label(world: World) -> None:
    """**引退・削除と違って教員が直せる。**

    あちらは他のコースが使っているものを取り上げる操作なので管理者に限るが、
    名前を直すのは取り上げる操作ではない。正しい名前を知っているのは科目の
    専門家で、キーは動かないので壊れない。
    """
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ルーブ"}
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/edit",
        data={"key": "cs.loops", "label": "ループ", "description": "繰り返しの制御"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "ループ" in page
    assert "繰り返しの制御" in page
    # キーは動かない。
    assert "cs.loops" in page


def test_the_key_is_not_editable_from_the_page(world: World) -> None:
    """キーを直せるように見せない。ID がキーから決まり、過去の採点が指している。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    form = page[page.index("名前・説明の修正") :]
    # 送るのは label と description だけ。key は hidden で固定。
    assert 'name="label"' in form
    assert 'name="description"' in form
    assert '<input type="hidden" name="key" value="cs.loops">' in form
    assert 'キー（<span class="mono">cs.loops</span>）は変わりません' in form


def test_the_course_can_narrow_which_components_it_uses(world: World) -> None:
    """**共有の語彙からの削除ではない。** 外しても知識要素は残る。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    for key, label in (("cs.loops", "ループ"), ("cs.python", "Python")):
        client.post(f"/manage/courses/{world.course.id}/kc", data={"key": key, "label": label})

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/scope",
        data={"kc": ["cs.loops"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == ("cs.loops",)
    # **このコースの一覧からは消える。** 使わないと決めたものが並び続けると、
    # 決めたこと自体が画面から読めない（#37）。
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "cs.loops" in page
    assert "cs.python" not in page

    # **語彙からは消えていない。** 他のコースの Q-matrix は壊れない。
    from aijudge_core import kc_id_for

    with world.database.unit_of_work() as uow:
        assert uow.skills.get_kc(kc_id_for("cs.python")) is not None


def test_a_component_left_out_can_be_brought_back(world: World) -> None:
    """隠すなら戻す道が要る。**「このコースに追加する」は登録と範囲の両方。**

    追加フォームは既にある知識要素をそのまま返す（何度押しても増えない）が、
    コースの範囲に入れなければ、絞っているコースでは一覧に出てこない。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    for key, label in (("cs.loops", "ループ"), ("cs.python", "Python")):
        client.post(f"/manage/courses/{world.course.id}/kc", data={"key": key, "label": label})
    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": ["cs.loops"]})
    assert "cs.python" not in client.get(f"/manage/courses/{world.course.id}/kc").text

    client.post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.python", "label": "Python"},
    )

    assert "cs.python" in client.get(f"/manage/courses/{world.course.id}/kc").text
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.knowledge_components == ("cs.loops", "cs.python")


def test_a_course_that_has_not_narrowed_still_sees_everything(world: World) -> None:
    """**「絞っていない」は「何も選んでいない」ではない。** 宣言するまで変えない。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    for key, label in (("cs.loops", "ループ"), ("cs.python", "Python")):
        client.post(f"/manage/courses/{world.course.id}/kc", data={"key": key, "label": label})

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "cs.loops" in page and "cs.python" in page
    with world.database.unit_of_work() as uow:
        # 追加しただけでは絞った状態にしない。
        assert uow.identity.get_course(world.course.id).knowledge_components == ()


def test_the_drafting_form_offers_only_the_selected_components(world: World) -> None:
    """**同じ名前空間を複数のコースが使うほど関係のない候補が増える。**

    C の科目に `cs.python.*` が並ぶのは見にくいだけでなく、誤った知識要素を
    課題に付けられるということでもある（設計原則 P6）。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    for key, label in (("cs.loops", "ループ"), ("cs.python", "Python")):
        client.post(f"/manage/courses/{world.course.id}/kc", data={"key": key, "label": label})

    unit = _unit_of(world)
    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "cs.python" in body  # 絞る前は両方出る

    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": ["cs.loops"]})
    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "cs.loops" in body
    assert "cs.python" not in body


def test_a_component_the_course_still_uses_stays_visible_when_unselected(
    world: World,
) -> None:
    """**外しても、このコースの課題が使っていれば一覧に残す。**

    消すと、その課題が何を問うているのかを画面から辿れなくなる。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec

    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            statement="## 課題 ##\n\n本文",
            knowledge_components=("cs.loops",),
        ),
        subject_profile="cs_intro_c",
        authored_by=world.register("t2", Role.INSTRUCTOR).user_id,
    )

    # 選択から外す。
    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": []})
    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": ["cs.nothing"]})
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "cs.loops" in page
    # このコースの課題が使っていることが分かる。
    assert "課題 1" in page


def test_an_unscoped_course_says_it_is_not_narrowed(world: World) -> None:
    """**全部を選んだ状態と、絞らないままは別物。**

    あとから名前空間に足された知識要素の扱いが変わる。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "まだ絞っていません" in page

    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": ["cs.loops"]})
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "使う知識要素を絞っています" in page


def test_only_an_admin_can_delete_a_component(world: World) -> None:
    """削除もコースをまたいで効く。1 コースの教員が他の語彙を消せない。"""
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.typo", "label": "打ち間違い"}
    )
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/delete", data={"key": "cs.typo"}
    )
    assert response.status_code == 403


def test_an_unused_component_is_deleted_from_the_page(world: World) -> None:
    """**打ち間違いの置き場所を引退にしない。**

    使われたことの無いキーを引退させて残すと、共有の一覧に誰の役にも
    立たない行が永久に並ぶ。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.typo", "label": "打ち間違い"}
    )
    assert "cs.typo" in client.get(f"/manage/courses/{world.course.id}/kc").text

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/delete",
        data={"key": "cs.typo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "cs.typo" not in client.get(f"/manage/courses/{world.course.id}/kc").text


def test_the_delete_control_is_hidden_for_a_used_component(world: World) -> None:
    """**使われているものには出さない。** 押せない操作を見せない。"""
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec

    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            statement="## 課題 ##\n\n本文",
            knowledge_components=("cs.loops",),
        ),
        subject_profile="cs_intro_c",
        authored_by=world.register("t2", Role.INSTRUCTOR).user_id,
    )

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert ">削除<" not in page
    # 直接叩いても消えない。
    response = client.post(f"/manage/courses/{world.course.id}/kc/delete", data={"key": "cs.loops"})
    assert response.status_code == 400
    assert "使われています" in response.json()["detail"]


def test_a_namespace_outside_the_profile_is_refused(world: World) -> None:
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "csci.loops", "label": "ループ"},
    )
    assert response.status_code == 400
    assert "名前空間" in response.json()["detail"]


def test_only_an_admin_can_retire_a_component(world: World) -> None:
    """引退はコースをまたいで効く。1 コースの教員が他の語彙を畳めない。"""
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/retire", data={"key": "cs.loops"}
    )
    assert response.status_code == 403


def _stub_writer(monkeypatch, *, fails: bool = False) -> None:
    from aijudge_admin.test_cases import GenerationResult
    from aijudge_authoring.drafting import DraftTestCase

    class _Writer:
        def __init__(self, *a, **kw) -> None: ...

        def write(self, statement, *, language="c", count=5):
            if fails:
                raise RuntimeError("connection refused")
            return GenerationResult(
                reference_solution="int main(void){return 0;}",
                test_cases=(
                    DraftTestCase(name="case1", input="1 2", expected="3"),
                    DraftTestCase(name="case2", input="2 3", expected="5"),
                ),
                prompt_id="test_cases_for_statement_ja@1",
                model="stub-model",
            )

    monkeypatch.setattr("aijudge_reviewconsole.manage.TestCaseWriter", _Writer)


def _add(client, course_id: str, unit: str, suffix: str, **extra):
    data = {
        "key_suffix": suffix,
        "unit": unit,
        "statement": f"## [必須] 課題 {suffix} ##\n\n2 つの整数を読み、和を出力しなさい。",
        "readability_weight": "0.3",
    }
    data.update(extra)
    return client.post(f"/manage/courses/{course_id}/tasks", data=data, follow_redirects=False)


def test_a_new_task_gets_test_cases_generated(monkeypatch, world: World) -> None:
    """**テストケースが無い課題は正しさが AI 判定に落ちる**（`auto_graded`）。

    テスト実行で確定できる科目なのに全課題が教員の確定待ちになるので、
    宣言している科目では用意する。
    """
    from aijudge_core import ReviewState

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch)

    assert _add(client, str(world.course.id), "ex04", "p1").status_code == 303
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        version = uow.tasks.latest_version(task.id)
    assert len(version.test_cases) == 2
    assert version.reference_solution
    # **承認まで出題されない。** 門は問題文の意図と合っているかを見ていない。
    assert version.provenance.review_state is ReviewState.IN_REVIEW


def test_an_instructor_can_refuse_the_generated_tests(monkeypatch, world: World) -> None:
    """C の科目にも設計を問う記述課題はある。強いる理由が無い。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch)

    assert _add(client, str(world.course.id), "ex04", "p1", no_auto_tests="1").status_code == 303
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        version = uow.tasks.latest_version(task.id)
    assert version.test_cases == ()


def test_a_task_without_tests_says_so_instead_of_saving_quietly(monkeypatch, world: World) -> None:
    """**黙って落とさない。** 保存できたことだけ伝えると、教員はテスト実行で
    確定する課題を作ったつもりのまま学期を過ごす。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch)

    response = _add(client, str(world.course.id), "ex04", "p1", no_auto_tests="1")
    assert "saved=task_without_tests" in response.headers["location"]
    body = client.get(response.headers["location"]).text
    assert "正しさは AI が判定します" in body


def test_an_existing_task_without_tests_is_flagged_when_opened(world: World) -> None:
    """#15 より前に作った課題にも出す。開いて分からなければ直す機会が無い。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        version = uow.tasks.latest_version(task.id)
        uow.tasks.save_version(
            version.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "c" * 32),
                    "version": version.version + 1,
                    "test_cases": (),
                }
            )
        )
        uow.commit()

    body = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "この課題にはテストケースがありません" in body


def test_a_report_subject_is_not_warned_about_missing_tests(world: World) -> None:
    """**宣言していない科目では、テストが無いのが正常。**

    落ちたわけでないものを同じ顔で警告すると、警告が読まれなくなる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(course.model_copy(update={"subject_profile": "report_ja"}))
        task = uow.tasks.list_for_course(world.course.id)[0]
        version = uow.tasks.latest_version(task.id)
        uow.tasks.save_version(
            version.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "d" * 32),
                    "version": version.version + 1,
                    "test_cases": (),
                }
            )
        )
        uow.commit()

    body = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "この課題にはテストケースがありません" not in body


def test_a_failed_generation_still_saves_the_task_and_says_so(monkeypatch, world: World) -> None:
    """**課題を作れなくしない**（設計原則 P2）。

    S6 が止まっているあいだ作問が止まると、教員は授業の準備そのものが
    できない。テストの無い課題として保存し、そうなったことを言う。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch, fails=True)

    response = _add(client, str(world.course.id), "ex04", "p1")
    assert response.status_code == 303
    assert "saved=task_generation_failed" in response.headers["location"]
    body = client.get(response.headers["location"]).text
    # **作れなかったことと、作らないことは別。** 前者は直せる。
    assert "テストケースを作れなかった" in body

    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        assert uow.tasks.latest_version(task.id).test_cases == ()


def test_a_generated_task_is_saved_awaiting_approval(monkeypatch, world: World) -> None:
    """**生成物は承認まで出題されない**（P5）。

    出所（`generated_by`）が版に載らないと `Provenance` は「教員が書いた」に
    なり、承認を経ずにそのまま出題可能になる。この経路は一度も通されて
    おらず、`save_task` が出所を受け取らないまま呼ばれていた。
    """
    from aijudge_authoring.drafting import DraftTestCase, TaskDraft
    from aijudge_core import ReviewState

    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )

    class _Drafter:
        def __init__(self, *a, **kw) -> None: ...

        def draft(self, blueprint, *, key):
            from aijudge_admin.drafting import DraftResult
            from aijudge_authoring.drafting import draft_to_spec

            draft = TaskDraft(
                title="生成された課題",
                statement="## 生成 ##\n\n2 つの整数を読み、和を出力しなさい。",
                reference_solution="int main(void){return 0;}",
                test_cases=(
                    DraftTestCase(name="case1", input="1 2", expected="3"),
                    DraftTestCase(name="case2", input="2 3", expected="5"),
                ),
            )
            return DraftResult(
                spec=draft_to_spec(draft, blueprint, key=key),
                draft=draft,
                prompt_id="task_draft_ja@2",
                model="stub-model",
            )

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskDrafter", _Drafter)

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/generate",
        data={"key_suffix": "p9", "kc": ["cs.loops"], "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        made = next(
            uow.tasks.latest_version(task.id)
            for task in uow.tasks.list_for_course(world.course.id)
            if uow.tasks.latest_version(task.id).provenance.generated_by
        )
    assert made.provenance.generated_by == "stub-model"
    assert made.provenance.generation_prompt_version == "task_draft_ja@2"
    # **承認まで出題されない。**
    assert made.provenance.review_state is ReviewState.IN_REVIEW
    assert not made.is_published


def test_generation_needs_a_registered_component(world: World) -> None:
    """AI に KC を作らせない。生成は登録済みからの選択だけ。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/generate",
        data={"key_suffix": "p9", "kc": ["cs.made.up"]},
    )
    assert response.status_code == 400


def test_the_unit_page_marks_a_task_that_is_not_approved(world: World) -> None:
    """**承認済みと同じ見た目で並べない。**

    一覧は `latest_version` をレビュー状態で絞らないので、生成したままの
    課題もここに出る。印が無いと、教員は並んでいる数をそのまま「この回の
    問題数」と読むが、実際に出題されるのは承認済みのぶんだけである。
    """
    from aijudge_core import ReviewState

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")
    unit = _unit_of(world)

    # 取り込んだ課題は承認済み。印は出ない。
    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "未承認 — 出題されません" not in body

    # **版は上書きせず足す**（P8）。生成した課題が届く形もこれで、
    # `latest_version` が新しい方を返す。
    from aijudge_core.ids import TaskVersionId

    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        version = uow.tasks.latest_version(task.id)
        uow.tasks.save_version(
            version.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "e" * 32),
                    "version": version.version + 1,
                    "provenance": version.provenance.model_copy(
                        update={
                            "generated_by": "stub",
                            "review_state": ReviewState.IN_REVIEW,
                        }
                    ),
                }
            )
        )
        uow.commit()

    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "未承認 — 出題されません" in body
    # そこから承認・却下へ行ける。
    assert f"/manage/courses/{world.course.id}/drafts" in body


def test_the_unit_page_offers_generation_only_with_components(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")

    body = client.get(f"/manage/courses/{world.course.id}/units/{_unit_of(world)}").text
    assert "知識要素が登録されていないので生成できません" in body

    world.register("boss", Role.ADMIN)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    body = client.get(f"/manage/courses/{world.course.id}/units/{_unit_of(world)}").text
    assert "AI に課題を作らせる" in body
    assert 'name="kc"' in body


# --------------------------------------------------------------------------
# 科目情報の URL とシラバスからの候補
# --------------------------------------------------------------------------


def _proposal(*keys: str):
    """候補を返す `SyllabusReader` の代わり。生成そのものは測らない。"""
    from aijudge_admin.syllabus import KcHint, ProposalResult, SyllabusProposal

    class _Reader:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def propose(self, text, *, namespaces, existing_keys=()):
            self.seen.append(text)
            return ProposalResult(
                proposal=SyllabusProposal(
                    knowledge_components=tuple(
                        KcHint(key=key, label=key.rsplit(".", 1)[-1]) for key in keys
                    )
                ),
                prompt_id="test",
                model="test",
            )

    return _Reader


def test_candidates_come_from_the_course_basics(monkeypatch, world: World) -> None:
    """**本文を貼り直させない。** 材料はコースが既に持っている。

    ここで貼らせると、2 つの経路で入った別々のシラバスがコースの中に並び、
    どちらが本当か分からなくなる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "## 到達目標\n\n配列を扱える"},
    )
    reader = _proposal("cs.arrays")
    monkeypatch.setattr("aijudge_reviewconsole.manage.SyllabusReader", reader)

    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    assert "cs.arrays" in body
    # 一覧と同じページに出る（体系を見ながら選べるように）。
    assert "知識要素を追加する" in body


def test_the_candidates_are_built_from_the_saved_description(monkeypatch, world: World) -> None:
    """渡しているのが本当にコースの基本情報であることを確かめる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": "計算機科学入門", "description": "ポインタと再帰を扱う"},
    )
    made = []

    class _Recording(_proposal("cs.pointers")):
        def propose(self, text, *, namespaces, existing_keys=()):
            made.append(text)
            return super().propose(text, namespaces=namespaces, existing_keys=existing_keys)

    monkeypatch.setattr("aijudge_reviewconsole.manage.SyllabusReader", _Recording)
    client.post(f"/manage/courses/{world.course.id}/kc/candidates")
    assert "ポインタと再帰を扱う" in made[0]
    assert "計算機科学入門" in made[0]


def test_candidates_need_the_basics_to_be_filled_in(world: World) -> None:
    """**候補を出せないことと、候補が無いことは違う。** 何をすればよいか言う。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(f"/manage/courses/{world.course.id}/kc/candidates")
    assert response.status_code == 400
    assert "基本情報" in response.json()["detail"]

    # 出せないときはボタンも出さず、基本情報への導線を出す。
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert f"/manage/courses/{world.course.id}/basics" in page


def test_an_already_registered_candidate_cannot_be_adopted_again(monkeypatch, world: World) -> None:
    """このコースで使用中のものは印だけ出して、選ばせない（押しても増えない）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "配列を扱える"},
    )
    # 第 1 階層は管理者の操作（`aijudge_admin.kc` の規則 2）。
    world.register("boss", Role.ADMIN)
    world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.arrays", "label": "配列"}
    )
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader", _proposal("cs.arrays", "cs.recursion")
    )
    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    rows = body[body.index("候補（") :]
    assert '<button type="submit" name="use" value="cs.recursion">' in rows
    assert '<button type="submit" name="use" value="cs.arrays">' not in rows
    assert "このコースで使用中" in rows
    # 名前はキーで結ぶ。位置で対応づけると、選ばなかった候補の名前が付く。
    assert 'name="label:cs.recursion"' in rows


def test_a_short_paste_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/candidates", data={"text": "短い"}
    )
    assert response.status_code == 400


def _candidate_form(*candidates, use: str) -> dict[str, object]:
    """候補の表が送る形。**全候補が隠し欄で、押した 1 件だけが `use`。**"""
    data: dict[str, object] = {"candidate": [key for key, _label, _desc in candidates], "use": use}
    for key, label, description in candidates:
        data[f"label:{key}"] = label
        data[f"description:{key}"] = description
    return data


def test_taking_a_candidate_into_the_form_registers_nothing(world: World) -> None:
    """**候補は素材であって成果物ではない。** 取り込んだだけでは何も入らない。

    KC の ID はキーから決まる（`kc_id_for`）ので、モデルの付けたキーが少しでも
    違えば直す道は無く、使われたあとは消すこともできない。だから登録の前に
    必ず人の手を通す。
    """
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc/draft",
        data=_candidate_form(("cs.arrays", "配列", "添字でたどるまとまり"), use="cs.arrays"),
    )
    assert response.status_code == 200

    from aijudge_core import kc_id_for

    with world.database.unit_of_work() as uow:
        assert uow.skills.get_kc(kc_id_for("cs.arrays")) is None


def test_the_form_is_filled_with_the_candidate_that_was_chosen(world: World) -> None:
    """**選んだ鍵に、その鍵の名前が付く。**

    隠し欄は全候補ぶん送られ、押されたのは 1 件だけ。位置で対応づけると、
    3 件中 3 件目を押したときに 1 件目の名前が入る（#33 で直したのと同じ形）。
    """
    world.register("boss", Role.ADMIN)
    body = (
        world.client("boss")
        .post(
            f"/manage/courses/{world.course.id}/kc/draft",
            data=_candidate_form(
                ("cs.loops", "繰り返し", "while と for"),
                ("cs.pointers", "ポインタ", "アドレスを持つ変数"),
                ("cs.arrays", "配列", "添字でたどるまとまり"),
                use="cs.arrays",
            ),
        )
        .text
    )
    form = body[body.index("知識要素を追加する") :]

    assert 'value="cs.arrays"' in form
    assert 'value="配列"' in form
    assert 'value="添字でたどるまとまり"' in form
    # 押していない候補の名前は入らない。
    assert 'value="繰り返し"' not in form
    assert "まだ登録されていません" in body


def test_a_component_this_course_does_not_use_is_offered_as_existing(
    monkeypatch, world: World
) -> None:
    """**体系にあるものを「新規」と出すのは嘘**（#41）。

    #37 で範囲外の知識要素を一覧から隠したので、候補がその唯一の入口になる。
    「新規」に見えると、押した教員は自分が体系に何を足したのかを誤解する。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "配列と再帰を扱える"},
    )
    for key, label in (("cs.arrays", "配列"), ("cs.loops", "ループ")):
        client.post(f"/manage/courses/{world.course.id}/kc", data={"key": key, "label": label})
    # このコースは cs.loops だけを使う ── cs.arrays は体系にあるが範囲外。
    client.post(f"/manage/courses/{world.course.id}/kc/scope", data={"kc": ["cs.loops"]})

    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader", _proposal("cs.arrays", "cs.recursion")
    )
    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    rows = body[body.index("候補（") :]

    assert "体系にあり" in rows
    # 範囲外でも取り込める（取り込めば範囲に入る）。
    assert '<button type="submit" name="use" value="cs.arrays">' in rows


def test_taking_an_existing_component_shows_the_name_the_vocabulary_has(world: World) -> None:
    """**体系の名前を出す。** `register` は既にあるものをそのまま返す。

    モデルの書いた名前を欄に入れると、教員はそこで直せると思い、直した内容は
    黙って捨てられる。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.arrays", "label": "配列", "description": "添字でたどるまとまり"},
    )

    body = client.post(
        f"/manage/courses/{world.course.id}/kc/draft",
        data=_candidate_form(("cs.arrays", "配列型データ", "モデルの言い換え"), use="cs.arrays"),
    ).text
    form = body[body.index("知識要素を追加する") :]

    assert 'value="配列"' in form
    assert "モデルの言い換え" not in form
    assert "名前と説明は変わりません" in body


def test_a_candidate_that_was_not_offered_is_refused(world: World) -> None:
    """取り込む候補が選ばれていないまま送られたら断る。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/kc/draft",
        data=_candidate_form(("cs.arrays", "配列", ""), use=""),
    )
    assert response.status_code == 400


def test_a_candidate_taken_into_the_form_still_follows_the_same_rules(world: World) -> None:
    """規則は手で足すときと同じ ── **取り込んだ先が同じ経路だから同じになる。**"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    body = client.post(
        f"/manage/courses/{world.course.id}/kc/draft",
        data=_candidate_form(("cs.loops.termination", "停止条件", ""), use="cs.loops.termination"),
    ).text
    assert 'value="cs.loops.termination"' in body

    # そのまま登録しようとすれば、親が無いことで断られる（`add_kc` の規則）。
    response = client.post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.loops.termination", "label": "停止条件"},
    )
    assert response.status_code == 400
    assert "親" in response.json()["detail"]


def test_the_course_settings_link_to_both_flows(world: World) -> None:
    """基本情報と知識要素は別の作業。入口も分ける。

    候補づくりは知識要素のページにあるが、**基本情報が入っていて初めて出る**
    （材料がそこにあるので）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    settings = client.get(f"/manage/courses/{world.course.id}").text
    assert f"/manage/courses/{world.course.id}/basics" in settings

    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "配列を扱える"},
    )
    kc_page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert f"/manage/courses/{world.course.id}/kc/candidates" in kc_page


def test_the_basics_page_saves_the_title_and_description(world: World) -> None:
    """基本情報はコースが持つ。**科目プロファイルには置かない**（ADR 0002）。

    あちらは採点の仕方の宣言で、コードと同じレビューを通す前提の設定である。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": "プログラミング及び実習 II", "description": "## 到達目標\n\n配列を使える"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.title == "プログラミング及び実習 II"
    assert "到達目標" in course.description


def test_the_basics_page_cannot_change_the_identity(world: World) -> None:
    """コードと学期はコースの同一性。変えると別のコースになる。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": "別名", "code": "other", "term": "2099-通年"},
    )
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.code == "prog2"
    assert course.term == "2025-後期"


def test_the_basics_page_shows_the_reading_indicator(world: World) -> None:
    """PDF の抽出とモデルの応答で十数秒かかる。何も出ないと押せたか分からない。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/basics").text
    assert "読み取り中" in body
    assert 'type="file"' in body
    # 読み取りのボタンはファイル選択と同じ行に置く。
    assert body.index('type="file"') < body.index(">読み取り<")
    # 読み取りと登録は別の操作。
    assert ">登録<" in body


def test_the_reading_indicator_starts_hidden(world: World) -> None:
    """`display` を持つクラスに `hidden` を付けても消えない。

    作者側の指定が利用者エージェントの `[hidden]{display:none}` に勝つので、
    明示的に打ち消しておかないと「読み取り中」が出っぱなしになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/basics").text
    assert 'class="working flash reading" hidden' in body
    assert "[hidden]{display:none!important}" in body


def test_the_long_running_forms_say_that_the_model_is_working(world: World) -> None:
    """**二度押しを止めるのが本題。** LLM の呼び出しは費用と待ち時間そのもの。

    仕組みは `base.html` に 1 つだけ置く。テンプレートごとに `onsubmit` を
    書き写していたので、**いちばん時間の掛かる作問に付いていなかった**。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    # 作問の欄は、問える知識要素が 1 つ以上あって初めて出る。
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    unit = _unit_of(world)

    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'data-working="生成中…' in body
    # 仕掛けは 1 か所（base.html）にあり、テンプレートは属性を書くだけ。
    assert "onsubmit=" not in body


def test_the_progress_is_not_reported_as_a_number(world: World) -> None:
    """**何%まで進んだかを知る手段が無い。** それらしい数字は根拠の無い表示になる。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    unit = _unit_of(world)

    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "<progress" not in body


def test_a_badge_that_needs_someone_is_not_the_same_as_a_bad_one(world: World) -> None:
    """**色は「人が何かする必要があるか」で決める**（#46）。

    引退した知識要素（放っておいてよい）と、教員が開かないと閉じないものが
    同じ赤で並んでいた。同じ見え方なら、どちらも目に留まらない。
    """
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/kc", data={"key": "cs.loops", "label": "ループ"}
    )
    client.post(f"/manage/courses/{world.course.id}/kc/retire", data={"key": "cs.loops"})

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    # 引退は確定した事実で、操作は要らない。
    assert '<span class="pill no">引退</span>' in page

    # **色だけに頼らない。** 記号を添える（色覚の差でも白黒でも読める）。
    assert ".pill.attn::before" in page


# --------------------------------------------------------------------------
# 実施中に課題を直す（#43）
# --------------------------------------------------------------------------


def test_the_page_does_not_tell_the_instructor_to_recreate_the_task(world: World) -> None:
    """**「作り直してください」と書かない。**

    作り直せば別の課題になり、問題セットに同じ問題が 2 件並んで、提出も採点も
    その 2 件に割れる ── 実際にそうなっていた（#43）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "作り直してください" not in page


def test_test_cases_can_be_added_to_a_task_that_has_none(monkeypatch, world: World) -> None:
    """**後から足せる。新しい版として。**

    足しただけでは何も変わらない ── 観点は自分の評価器を持っており、テストの
    無い課題では正しさが AI 判定になっている。**正しさをテスト実行に戻す**
    ところまでが「テストケースを付ける」である。

    できるのは承認待ちの版で、**承認するまで学習者にはいまの版が出続ける**
    （#48）。門は「参照解答とテストが整合している」までしか言わない。
    """
    from aijudge_core import ReviewState
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch)

    # 自動テストを使わない課題として作る（テストケースを持たない）。
    assert _add(client, str(world.course.id), "ex04", "p1", no_auto_tests="1").status_code == 303
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        before = uow.tasks.latest_version(task.id)
    assert before.test_cases == ()

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "テストケースを後から付ける" in page

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/test-cases", follow_redirects=False
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(str(task.id)))
        published = uow.tasks.latest_published_version(TaskId(str(task.id)))
    # 新しい版になり、古い版は書き換わっていない（P8）。
    assert after.version == before.version + 1
    assert after.test_cases
    # **正しさをテスト実行に戻す。** 戻さなければ、テストは作られたのに
    # 誰も実行しない。
    correctness = next(c for c in after.criteria if c.code == "correctness")
    assert correctness.evaluator_id == "code_test_runner"
    # 承認待ち。学習者には 1 つ前の承認済みが出続ける。
    assert after.provenance.review_state is ReviewState.IN_REVIEW
    assert published.id == before.id


def test_a_failed_generation_leaves_the_task_alone(monkeypatch, world: World) -> None:
    """**課題を壊さない**（P2）。作れなかったことと、作らないことは別。"""
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _stub_writer(monkeypatch)
    assert _add(client, str(world.course.id), "ex04", "p1", no_auto_tests="1").status_code == 303
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        before = uow.tasks.latest_version(task.id)

    _stub_writer(monkeypatch, fails=True)
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/test-cases", follow_redirects=False
    )
    assert response.status_code == 303
    assert "tests_failed" in response.headers["location"]

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(str(task.id)))
    assert after.version == before.version


def test_a_generated_revision_is_not_approved_by_itself(monkeypatch, world: World) -> None:
    """**訂正で生成した中身も承認待ちにする。**

    `save_task` は訂正のとき版を作り直すが、そこに出所を渡していなかったので
    生成物が「教員が書いた」ことになり、承認を経ずに出題されていた。
    観点を宣言する課題（コースが共通ルーブリックを持てばそうなる）では、
    `_declared_version` が `APPROVED` を直に書いてもいた ── 生成した課題が
    承認の導線を丸ごと素通りする経路だった（設計原則 P5）。
    """
    from aijudge_authoring import TaskSpec, build_task_version
    from aijudge_core import ReviewState
    from aijudge_core.ids import UserId

    spec = TaskSpec(
        key="ex09/p1",
        statement="## 課題 ##\n\n書きなさい。",
        criteria=(
            {
                "code": "correctness",
                "title": "正しさ",
                "description": "仕様どおりか。",
                "weight": 1.0,
                "levels": [
                    {"level": 0, "label": "未達", "descriptor": "違う", "score_ratio": 0.0},
                    {"level": 1, "label": "達成", "descriptor": "よい", "score_ratio": 1.0},
                ],
            },
        ),
    )
    version = build_task_version(
        spec,
        course_id=world.course.id,
        subject_profile="cs_intro_c",
        authored_by=UserId("usr_" + "1" * 32),
        generated_by="stub-model",
        generation_prompt_version="p@1",
    )
    assert version.provenance.review_state is ReviewState.IN_REVIEW
    assert version.provenance.generated_by == "stub-model"


def test_the_list_says_which_version_and_when_it_was_made(world: World) -> None:
    """**同じ題名が並んだとき、新旧が読める必要がある**（#54）。

    版番号だけでは「いつの版か」が分からない。`TaskVersion.created_at` は
    再現性のために既に記録している値で、出していなかっただけである。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    unit = _unit_of(world)

    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import TaskId

        version = uow.tasks.latest_version(TaskId(task_id))

    listing = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert version.created_at.strftime("%m-%d %H:%M") in listing

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert version.created_at.strftime("%Y-%m-%d %H:%M") in page


def test_a_task_without_submissions_can_be_deleted(world: World) -> None:
    """**打ち間違いは消せる。** 知識要素と同じ区別（#51）。"""
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "この課題を削除" in page

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)) is None
        assert uow.tasks.latest_version(TaskId(task_id)) is None


def test_a_task_can_be_withdrawn_and_brought_back(world: World) -> None:
    """**取り下げは削除ではない。** 学習者に出なくなるが、記録は残る。"""
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    unit = _unit_of(world)

    assert (
        client.post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/withdraw", follow_redirects=False
        ).status_code
        == 303
    )
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).withdrawn
        # 版は消えていない。
        assert uow.tasks.latest_version(TaskId(task_id)) is not None

    # 教員の一覧には残り、取り下げたことが読める。
    listing = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "出題を取り下げ済み" in listing

    # 押し間違いは取り消せる。
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/withdraw", data={"restore": "1"}
    )
    with world.database.unit_of_work() as uow:
        assert not uow.tasks.get_task(TaskId(task_id)).withdrawn


def test_a_set_says_when_two_tasks_share_a_title(world: World) -> None:
    """**同じ題名が並んでいることを出す。** 別々の課題なので提出も採点も割れる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    unit = _unit_of(world)

    with world.database.unit_of_work() as uow:
        original = uow.tasks.list_for_course(world.course.id)[0]

    # 同じ題名の課題をもう 1 件足す（「作り直した」あとの状態）。
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "unit": original.unit or "_",
            "key_suffix": "again",
            "statement": f"## {original.title} ##\n\n同じ題名の別の課題。",
            "no_auto_tests": "1",
        },
    )

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "同じ題名の課題が" in page


def test_a_regrade_is_offered_only_when_something_is_on_an_older_version(world: World) -> None:
    """**訂正しただけでは、既に出ている提出は古い版のまま。**

    自動で積み直すと、誰も押していない再採点で成績が動く（P5）。押せる形で
    出し、押されたときだけ動かす。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)

    # 提出も採点も無いので、採点し直すものは無い。
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "いまの版で再採点" not in page


# --------------------------------------------------------------------------
# 受講者（別ページ）
# --------------------------------------------------------------------------


def test_the_enrolments_have_their_own_page(world: World) -> None:
    """受講 100 名規模。設定を 1 つ直しに来た教員に 100 行めくらせない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    menu = client.get(f"/courses/{world.course.id}").text
    assert f"/manage/courses/{world.course.id}/enrolments" in menu

    page = client.get(f"/manage/courses/{world.course.id}/enrolments").text
    assert "受講登録" in page
    # 龍大の認証を使うので、メールアドレスは扱わない。
    assert "メールアドレス" not in page or "扱いません" in page


def test_the_enrolment_pages_break_the_headcount_down_by_role(world: World) -> None:
    """**0 名の役割も出す。** 総数だけでは、TA を登録し忘れているのか
    0 名が正しいのかが読み取れない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("s2400001", Role.LEARNER)
    world.register("s2400002", Role.LEARNER)
    client = world.client("teacher")

    # 受講者のページ: **0 名の役割も並べる。** 総数だけでは、TA を登録し
    # 忘れているのか 0 名が正しいのかが読み取れない。
    page = client.get(f"/manage/courses/{world.course.id}/enrolments").text
    rows = page[page.index('<ul class="rolecounts">') :]
    assert ">2</b>" in rows  # learner
    assert ">1</b>" in rows  # instructor
    assert ">0</b>" in rows  # assistant / admin は 0 名でも並ぶ
    assert "assistant" in rows

    # コースの入口: 行の高さを保つため 0 名は出さない（内訳の確認は上のページ）。
    menu = client.get(f"/courses/{world.course.id}").text
    assert "3 名" in menu  # 教員 1 + 学習者 2
    assert "learner 2" in menu
    assert "instructor 1" in menu
    # 0 名の役割はここには出さない（確認は受講者のページ）。
    assert "assistant 0" not in menu


def test_the_enrolment_form_explains_the_roles_as_differences(world: World) -> None:
    """**差分で書く。** 4 つを列挙すると同じ項目が 4 回並び、どこが違うのかを
    読み手が引き算することになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text
    table = page[page.index("下位の役割に加えてできること") :]
    for role in ("learner", "assistant", "instructor", "admin"):
        assert role in table


def test_the_console_does_not_offer_admin_or_instructor_to_a_teacher(world: World) -> None:
    """**教員が配れるのは assistant まで**（#126。#100 時点では instructor まで
    だったが、教員どうしが際限なく教員を増やせるのを避けるため狭めた）。

    `admin` はコースを作れてテナント内の全コースに届く。担当教員が自分の
    コースの受講者一覧から配れる権限ではない。以前は `Role` の全値を選択肢に
    していたので、`assistant` と `instructor` の間に `admin` が並んでいた。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    # 選択肢に無い（役割の変更・名簿の既定のどちらにも）。
    options = {line for line in page.splitlines() if "<option" in line}
    assert not [line for line in options if 'value="admin"' in line], "admin が選択肢にある"
    assert not [line for line in options if 'value="instructor"' in line], (
        "教員に instructor が選択肢として出ている"
    )
    for role in ("learner", "assistant"):
        assert [line for line in options if f'value="{role}"' in line], f"{role} が選べない"


def test_the_console_offers_instructor_to_an_admin(world: World) -> None:
    """**管理者が配れるのは instructor まで**（#126）。"""
    world.register("boss", Role.ADMIN)
    page = world.client("boss").get(f"/manage/courses/{world.course.id}/enrolments").text
    options = {line for line in page.splitlines() if "<option" in line}
    for role in ("learner", "assistant", "instructor"):
        assert [line for line in options if f'value="{role}"' in line], f"{role} が選べない"
    assert not [line for line in options if 'value="admin"' in line], "admin が選択肢にある"


def test_admin_cannot_be_granted_through_the_form(world: World) -> None:
    """**画面で塞ぐだけにしない。** 選択肢を減らしても POST は手で作れる。"""
    world.register("teacher", Role.INSTRUCTOR)
    student = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "admin"},
    )
    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.LEARNER, "役割が上がっている"

    # 教員の上限は assistant（#126）。instructor は教員には付与できない。
    denied = client.post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "instructor"},
    )
    assert denied.status_code == 403
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.LEARNER, "役割が上がっている"

    # assistant までは通る。
    ok = client.post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "assistant"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.ASSISTANT


def test_an_admin_can_promote_someone_to_instructor(world: World) -> None:
    """**管理者は instructor まで付与できる**（#126、教員は assistant まで）。"""
    world.register("boss", Role.ADMIN)
    student = world.register("s2400001", Role.LEARNER)
    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "instructor"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.INSTRUCTOR


def test_an_admin_can_manage_a_course_they_are_not_enrolled_in(world: World) -> None:
    """管理者は「テナント内のどこかで ADMIN」であれば、そのコースの受講者で
    なくても教員を昇格させられる（#126）。付与できる前提が「そのコースの
    メンバーであること」だと、管理者は自分がまだ触っていないコースに
    教員を配れなくなる。
    """
    other_course, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="other",
        title="別コース",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    boss = world.register("boss", Role.ADMIN, course_id=other_course.id)
    student = world.register("s2400001", Role.LEARNER)

    with world.database.unit_of_work() as uow:
        # boss はこのコース（world.course）には受講登録されていない。
        assert uow.identity.find_enrollment(world.course.id, boss.user_id) is None

    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "instructor"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.INSTRUCTOR


def test_admin_cannot_be_granted_through_a_pasted_roster(world: World) -> None:
    """**名簿の行にも役割が書ける**（4 列目）。既定だけ見ると素通りする。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("s2400002", None)
    client = world.client("teacher")

    line = "s2400002 s2400002@mail.example.jp - admin"
    response = client.post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": line, "role": "learner"},
    )
    assert response.status_code == 403, "名簿の中の admin が通った"

    # 既定の役割としても通らない。
    assert (
        client.post(
            f"/manage/courses/{world.course.id}/enrolments",
            data={"roster": "s2400002", "role": "admin"},
        ).status_code
        == 403
    )


def test_an_existing_admin_is_shown_but_not_editable(world: World) -> None:
    """**付けられない権限は外せない**（#100）。外せると、担当教員が管理者を
    自分のコースから締め出せることになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    boss = world.register("chief", Role.ADMIN)
    client = world.client("teacher")

    page = client.get(f"/manage/courses/{world.course.id}/enrolments").text
    assert "chief" in page, "管理者が一覧から消えている"
    assert "画面からは変更不可" in page

    response = client.post(
        f"/manage/courses/{world.course.id}/enrolments/{boss.user_id}/role",
        data={"role": "learner"},
    )
    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, boss.user_id)
    assert enrollment is not None and enrollment.role is Role.ADMIN


def test_the_role_explanations_match_what_the_code_enforces(world: World) -> None:
    """**説明と権限がずれないようにする。** ずれた説明は無いより悪い。

    ここで確かめるのは、画面に書いた区切りが実装の区切りと同じであること。
    """
    from aijudge_core import Enrollment
    from aijudge_core import Role as R
    from aijudge_core.ids import CourseId as CId
    from aijudge_core.ids import UserId as UId

    def _enrollment(role: R) -> Enrollment:
        return Enrollment(
            tenant_id=TenantId("ten_" + "0" * 32),
            course_id=CId("crs_" + "0" * 32),
            user_id=UId("usr_" + "0" * 32),
            role=role,
        )

    # 採点できるのは learner 以外（`Enrollment.can_grade`）。
    assert not _enrollment(R.LEARNER).can_grade
    assert _enrollment(R.ASSISTANT).can_grade
    assert _enrollment(R.INSTRUCTOR).can_grade
    assert _enrollment(R.ADMIN).can_grade

    # コースの設定は TA には開けない（`_require_instructor`）。
    world.register("ta", Role.ASSISTANT)
    assert world.client("ta").get(f"/manage/courses/{world.course.id}").status_code == 403


def test_the_course_settings_page_no_longer_holds_the_enrolments(world: World) -> None:
    """受講者は自分のページを持つので、設定の中に二重に置かない。

    同じものが 2 か所にあると、片方だけ直したときにもう片方が古いまま残る。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    settings = client.get(f"/manage/courses/{world.course.id}").text
    assert '<ul class="rolecounts">' not in settings
    assert f"/manage/courses/{world.course.id}/enrolments" not in settings
    # 入口からは行ける。
    menu = client.get(f"/courses/{world.course.id}").text
    assert f"/manage/courses/{world.course.id}/enrolments" in menu


def test_the_role_breakdown_ignores_the_search_filter(world: World) -> None:
    """内訳は**絞り込みの前**の数。絞り込んだ結果の内訳を出すと、
    「TA が 0 名」が登録漏れなのか絞り込みの結果なのか分からない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("s2400001", Role.LEARNER)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments?q=zzz").text
    rows = page[page.index("rolecounts") :]
    # 絞り込みは 0 件でも、内訳は登録済みの数を出す。
    assert ">1</b>" in rows


def test_the_syllabus_is_rendered_as_markdown_and_folded(world: World) -> None:
    """素のまま出すと見出しも箇条書きも記号のまま並ぶ（課題文で実際に起きた）。

    畳んで置くのは、開いたままだと下にある自動確定・提出形式・採点設定に
    たどり着くのに毎回スクロールすることになるため。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "## 到達目標\n\n- 配列を使える"},
    )
    page = client.get(f"/manage/courses/{world.course.id}").text
    assert '<details class="syllabus"' in page
    assert "<li>配列を使える</li>" in page
    # 記号のまま出ていない。
    assert "## 到達目標" not in page


def test_the_syllabus_never_carries_raw_html(world: World) -> None:
    """本文はいずれモデルの出力にもなる（#3）。`<script>` を通す経路を作らない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "概要<script>alert(1)</script>"},
    )
    page = client.get(f"/manage/courses/{world.course.id}").text
    assert "<script>alert(1)</script>" not in page


def test_a_course_without_a_syllabus_says_so(world: World) -> None:
    """空欄は「未入力」と書く。畳んだ見出しだけ出ていると、
    開けば何かあるように見える。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "概要・到達目標は未入力です" in page


def test_a_role_can_be_changed_afterwards(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    student = world.register("s2400001", Role.LEARNER)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "assistant"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert AuthService(uow.identity).role_in(world.course.id, student.user_id) is Role.ASSISTANT


def test_an_instructor_cannot_change_their_own_role(world: World) -> None:
    """学習者に落とすとコースが見えなくなり、戻す手段が無い。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{teacher.user_id}/role",
        data={"role": "learner"},
    )
    assert response.status_code == 400


def test_the_enrolments_can_be_filtered_by_prefix(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("y239001", Role.LEARNER)
    world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    body = client.get(f"/manage/courses/{world.course.id}/enrolments?q=y23").text
    assert "y239001" in body
    assert "s2400001" not in body


# --------------------------------------------------------------------------
# 採点設定（コースごとの上書き）
# --------------------------------------------------------------------------


def test_grading_settings_are_scoped_to_the_course(world: World) -> None:
    """**このコースにしか効かない。** だから教員が画面から変えてよい。"""
    world.register("teacher", Role.INSTRUCTOR)
    other, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="prog1",
        title="プログラミング及び実習 I",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    world.register("other_teacher", Role.INSTRUCTOR, other.id)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/grading",
        data={"language": "python", "action": "save"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        mine = uow.identity.get_course(world.course.id)
        theirs = uow.identity.get_course(other.id)
    assert mine.grading_overrides["evaluator_options"]["code_test_runner"]["language"] == "python"
    # 同じ雛形を使う別のコースには効かない。
    assert theirs.grading_overrides == {}


def test_an_empty_field_leaves_the_template_alone(world: World) -> None:
    """空欄は「雛形のまま」。0 として保存すると区別が付かなくなる。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/grading",
        data={"language": "", "timeout_seconds": "", "action": "save"},
    )
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.grading_overrides == {}


def test_a_broken_setting_is_refused(world: World) -> None:
    """保存時に起動時と同じ検査を通す。通らないものは入らない。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/grading",
        data={"timeout_seconds": "-5", "action": "save"},
    )
    assert response.status_code == 400


def test_the_settings_page_offers_a_trial(world: World) -> None:
    """`language` の取り違えは設定の検査では捕まらない。試す道具を置く。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "この設定で試行" in body
    assert "このコースだけに効きます" in body


def test_an_assistant_cannot_change_the_grading_settings(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/grading", data={"language": "python"}
    )
    assert response.status_code == 403


def test_the_grading_settings_explain_each_evaluator(world: World) -> None:
    """名前だけでは何をするか分からない。説明は評価器が持つ（docstring）。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "コンパイルして実行し" in body
    assert "ルーブリック観点を LLM に判定させる" in body


def test_the_grading_settings_say_where_the_rubric_lives(world: World) -> None:
    """ルーブリックの観点は課題ごと。ここには無い、と書いておく。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "共通ルーブリック" in body
    assert "ここには観点の設定はありません" in body


def test_the_compile_and_review_limits_can_be_set(world: World) -> None:
    """数値計算の課題では実行もコンパイルも伸ばす。合否境界も科目で違う。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/grading",
        data={
            "case_timeout_seconds": "30",
            "compile_timeout_seconds": "60",
            "samples": "5",
            "boundary_score": "0.7",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    runner = course.grading_overrides["evaluator_options"]["code_test_runner"]
    assert runner["case_timeout_seconds"] == 30
    assert runner["compile_timeout_seconds"] == 60
    assert course.grading_overrides["evaluator_options"]["rubric_ai_judge"]["samples"] == 5
    assert course.grading_overrides["review_policy"]["boundary_score"] == 0.7


def test_a_ratio_outside_the_range_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/grading",
        data={"boundary_score": "1.5", "action": "save"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# ルーブリック（コース共通と課題ごと）
# --------------------------------------------------------------------------


def _rubric_form(*rows) -> dict[str, list[str]]:
    """観点の表をフォームの形にする。

    同名のフィールドが行数ぶん並ぶ（画面の表がそうなっている）ので、
    **値を並びで渡す** ── タプルの並びで渡すと httpx が本文に載せない。
    """
    return {
        "criterion_code": [code for code, _t, _w, _l in rows],
        "criterion_title": [title for _c, title, _w, _l in rows],
        "criterion_description": [title for _c, title, _w, _l in rows],
        "criterion_weight": [weight for _c, _t, weight, _l in rows],
        "criterion_evaluator": ["" for _row in rows],
        "criterion_order": [str(index + 1) for index, _row in enumerate(rows)],
        "criterion_levels": [levels for _c, _t, _w, levels in rows],
    }


def test_the_rubric_is_saved_in_the_order_the_instructor_gave() -> None:
    """**並びが評価順である**（AND のとき上から評価して 0% で打ち切る）。

    画面の行の並びではなく、行に書いた「評価順」で決める ── 上下ボタンだと
    1 手ごとに保存が要り、10 観点を並べ替えるのに 10 往復になる。
    """
    from aijudge_admin import rubric

    rows = [
        {"code": "readable", "title": "読める", "weight": "0.4", "order": "2", "levels": ""},
        {"code": "runs", "title": "動く", "weight": "0.6", "order": "1", "levels": ""},
    ]
    assert [c.code for c in rubric.parse(rows)] == ["runs", "readable"]
    # 画面に返すときは 1 から振り直す（間に挿すために小数を書かせない）。
    assert [row["order"] for row in rubric.to_rows(rubric.parse(rows))] == [1, 2]


def test_a_course_can_declare_how_its_criteria_are_folded(world: World) -> None:
    """AND / OR はルーブリック単位の設定。**課題が指定すればそちらが勝つ。**"""
    world.register("teacher", Role.INSTRUCTOR)
    data = _rubric_form(("structure", "構成", "0.5", ""), ("discussion", "考察", "0.5", ""))
    data["aggregation"] = ["and"]
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/rubric", data=data, follow_redirects=False
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.rubric_aggregation == "and"


def test_an_unknown_way_of_folding_is_refused(world: World) -> None:
    """**黙って OR に倒さない。** 倒すと、AND のつもりの課題が重み付き和になる。"""
    world.register("teacher", Role.INSTRUCTOR)
    data = _rubric_form(("structure", "構成", "1.0", ""))
    data["aggregation"] = ["xor"]
    response = world.client("teacher").post(f"/manage/courses/{world.course.id}/rubric", data=data)
    assert response.status_code == 400


def test_a_course_can_declare_a_shared_rubric(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(
            ("structure", "構成", "0.5", ""),
            ("discussion", "考察", "0.5", ""),
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert [c["code"] for c in course.rubric] == ["structure", "discussion"]
    # 段階を書かなければ 4 段の既定が入る。
    assert len(course.rubric[0]["levels"]) == 4


def test_weights_that_do_not_add_up_are_refused(world: World) -> None:
    """観点ごとの重みが成績の配分そのもの。合計 1.0 でないと成立しない。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(("a", "A", "0.5", ""), ("b", "B", "0.9", "")),
    )
    assert response.status_code == 400
    assert "1.0" in response.json()["detail"]


def test_a_new_task_inherits_the_course_rubric(world: World) -> None:
    """レポートの観点を課題ごとに書き写させない（写し間違いが増えるだけ）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(("structure", "構成", "0.6", ""), ("discussion", "考察", "0.4", "")),
    )
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] レポート ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        version = uow.tasks.latest_version(task.id)
    assert [c.code for c in version.criteria] == ["structure", "discussion"]


def test_an_existing_task_rubric_can_be_edited(world: World) -> None:
    """出題済みの版は書き換えず、版を上げる（P8）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 課題 ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 課題 ##\n\n本文",
            "readability_weight": "0.3",
            **_rubric_form(
                ("correctness", "正しさ", "0.5", ""),
                ("design", "設計", "0.5", "だめ | 追えない | 0\nよい | 追える | 1.0"),
            ),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
        original = uow.tasks.get_version(first.id)
    assert latest.version == first.version + 1
    assert [c.code for c in latest.criteria] == ["correctness", "design"]
    design = next(c for c in latest.criteria if c.code == "design")
    assert len(design.levels) == 2
    # 元の版はそのまま残る。
    assert [c.code for c in original.criteria] == ["correctness", "readability"]


def test_the_rubric_editor_is_on_both_screens(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")
    assert "共通ルーブリック" in client.get(f"/manage/courses/{world.course.id}").text
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "この課題の観点" in page
    assert 'name="criterion_code"' in page


def test_each_criterion_is_folded_away(world: World) -> None:
    """開いていない観点は触れない。直すつもりのないものを誤って書き換えない。"""
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert 'class="criterion"' in body
    # 追加は明示的に開いてから。常に空の行を出さない。
    assert "＋ 観点を追加する" in body
    assert body.count('name="criterion_code"') == 3  # 既定の 2 観点 + 追加の 1


def test_the_task_editor_uses_the_full_width(world: World) -> None:
    """ルーブリックは成績の配分そのもの。狭い列に押し込むと段階が読めない。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    body = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    )
    assert 'name="criterion_levels"' in body
    # 出題順は打たせない（一覧の並び替えで決める）。
    assert 'name="position"' not in body


def test_tasks_are_reordered_from_the_list(world: World) -> None:
    """**数字を打たせない。** 1 問差し込むたびに全部を打ち直すことになる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    for suffix in ("p1", "p2"):
        client.post(
            f"/manage/courses/{world.course.id}/tasks",
            data={
                "key_suffix": suffix,
                "unit": "ex04",
                "statement": f"## [必須] 課題 {suffix} ##\n\n本文",
                "readability_weight": "0.3",
            },
        )
    with world.database.unit_of_work() as uow:
        ordered = sorted(uow.tasks.list_for_course(world.course.id), key=lambda t: t.sort_key)
    second = ordered[1]

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{second.id}/move",
        data={"direction": "up"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        again = sorted(uow.tasks.list_for_course(world.course.id), key=lambda t: t.sort_key)
    assert again[0].id == second.id


def _add_task(client, course_id: str, unit: str, suffix: str) -> None:
    client.post(
        f"/manage/courses/{course_id}/tasks",
        data={
            "key_suffix": suffix,
            "unit": unit,
            "statement": f"## [必須] 課題 {suffix} ##\n\n本文",
            "readability_weight": "0.3",
        },
    )


def test_a_task_moves_to_another_unit_and_takes_that_unit_schedule(world: World) -> None:
    """**日程は移動先に揃える。** セットの中で締切がずれると、学習者にも
    教員にも「この回はいつまでか」が言えなくなる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(client, str(world.course.id), "ex04", "p1")
    _add_task(client, str(world.course.id), "ex05", "p1")
    client.post(
        f"/manage/courses/{world.course.id}/units/ex05/schedule",
        data={
            "opens_at": "2026-09-01T09:00",
            "submissions_open_at": "",
            "due_at": "2026-09-08T23:59",
        },
    )
    with world.database.unit_of_work() as uow:
        moving = next(t for t in uow.tasks.list_for_course(world.course.id) if t.unit == "ex04")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{moving.id}/unit",
        data={"unit": "ex05"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        moved = uow.tasks.get_task(moving.id)
        head = next(
            t
            for t in uow.tasks.list_for_course(world.course.id)
            if t.unit == "ex05" and t.id != moving.id
        )
    assert moved.unit == "ex05"
    assert moved.due_at == head.due_at
    assert moved.opens_at == head.opens_at
    # 並びは移動先の末尾。**番号を持たない課題も数に入れる** ── 画面から
    # 足した課題は `position` が空なので、番号だけ見ると先頭に入ってしまう。
    assert head.position is None
    assert moved.position == 2


def test_moving_a_task_keeps_its_identity(world: World) -> None:
    """**移動しても同じ課題のまま。** `TaskId` は課題キーから導かれるので
    （`derived_id("tsk", key)`）、鍵が動けばそれは別の課題であり、過去の
    提出との対応が切れる（P8）。移動は所属だけを変える。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(client, str(world.course.id), "ex04", "p1")
    _add_task(client, str(world.course.id), "ex05", "p1")
    with world.database.unit_of_work() as uow:
        moving = next(t for t in uow.tasks.list_for_course(world.course.id) if t.unit == "ex04")
        before = uow.tasks.latest_version(moving.id).source_key

    client.post(f"/manage/courses/{world.course.id}/tasks/{moving.id}/unit", data={"unit": "ex05"})
    with world.database.unit_of_work() as uow:
        # 同じ ID で引けること自体が、鍵が動いていないことの確認になる。
        moved = uow.tasks.get_task(moving.id)
        assert moved is not None
        assert uow.tasks.latest_version(moved.id).source_key == before


def test_a_task_cannot_be_moved_to_the_unit_it_is_already_in(world: World) -> None:
    """押しても何も起きない操作を選択肢に出さない（画面でも候補から外している）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(client, str(world.course.id), "ex04", "p1")
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/unit", data={"unit": "ex04"}
    )
    assert response.status_code == 400


def test_the_move_form_lists_only_the_other_units(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(client, str(world.course.id), "ex04", "p1")
    _add_task(client, str(world.course.id), "ex05", "p1")
    with world.database.unit_of_work() as uow:
        task = next(t for t in uow.tasks.list_for_course(world.course.id) if t.unit == "ex04")
    body = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    form = body[body.index("別の問題セットへ移す") :]
    assert 'value="ex05"' in form
    assert 'value="ex04"' not in form


def test_the_task_page_says_whether_the_rubric_is_the_course_one(world: World) -> None:
    """同じに見えて違う、が最も困る。既定と同じかどうかを示す。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(("correctness", "正しさ", "0.7", ""), ("style", "書き方", "0.3", "")),
    )
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 課題 ##\n\n本文",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    page = f"/manage/courses/{world.course.id}/tasks/{task.id}/edit"
    assert "コースの共通ルーブリックと同じ" in client.get(page).text

    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 課題 ##\n\n本文",
            **_rubric_form(("only", "唯一", "1.0", "")),
        },
    )
    assert "この課題だけの観点になっています" in client.get(page).text


def test_a_task_rubric_can_go_back_to_the_course_one(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(("correctness", "正しさ", "0.7", ""), ("style", "書き方", "0.3", "")),
    )
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 課題 ##\n\n本文",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 課題 ##\n\n本文",
            **_rubric_form(("only", "唯一", "1.0", "")),
        },
    )

    # **押した時点では保存しない**（#58）。画面は共通ルーブリックの欄を
    # `<template>` で持ち、ボタンは編集中の欄をそれで置き換えるだけである。
    # 以前はここが独立した POST で、押すだけで版が上がっていた ── 編集の
    # 途中で押すと、書いた内容は保存されないまま別の版ができた。
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert 'id="course-rubric-rows"' in page, "共通ルーブリックの欄が画面に無い"
    assert 'data-replace-target="#rubric-editor"' in page
    template = page[page.index('id="course-rubric-rows"') :]
    assert 'value="correctness"' in template and 'value="style"' in template

    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(task.id)
    assert [c.code for c in before.criteria] == ["only"], "画面を開いただけで版が動いた"

    # 反映は「保存」で行う。
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 課題 ##\n\n本文",
            **_rubric_form(("correctness", "正しさ", "0.7", ""), ("style", "書き方", "0.3", "")),
        },
    )
    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert [c.code for c in latest.criteria] == ["correctness", "style"]


def test_destructive_actions_ask_before_they_run(world: World) -> None:
    """削除・取り下げ・移動は押しただけでは走らない（#58）。

    確認が無いのは実際に取り消せない操作にとって危うい ── 削除は課題と
    全版が消え、戻す手段が無い。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text

    assert 'id="confirm-dialog"' in page, "確認ダイアログが画面に無い"
    assert page.count("data-confirm=") >= 3, "確認が付いていない破壊的操作がある"
    assert "取り消せません" in page, "削除の取り返しのつかなさが書かれていない"

    # **並びは影響が戻せる順**（#58）。移動は日程が変わるだけ、取り下げは
    # 取り消せる、削除は取り消せない。
    assert page.index("/unit") < page.index("/withdraw") < page.index("/delete"), (
        "移動・取り下げ・削除の並びが違う"
    )


# --------------------------------------------------------------------------
# 問題セットを丸ごと片付ける（#59）
# --------------------------------------------------------------------------


def test_the_unit_page_states_the_breakdown_before_it_is_pressed(world: World) -> None:
    """**押してからでないと分からないのでは確認にならない。**

    1 回の操作で課題ごとに結果が変わる（提出が無ければ削除、あれば取り下げ）
    ので、削除が何件で取り下げが何件かを押す前に出す。
    """
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert "/clear" in page, "片付ける導線が無い"
    assert "この問題セットを片付ける" in page
    # 提出がまだ無いので、全件が削除の見込みとして出る。
    assert "提出が 1 件も無い 1 件は削除" in page
    assert "data-confirm=" in page, "確認なしで消せてしまう"


def test_clearing_a_unit_deletes_what_is_unused(world: World) -> None:
    """規則は `aijudge_admin.tasks` に置いてあり、画面はそれを呼ぶだけ。"""
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/clear", follow_redirects=False
    )
    assert response.status_code == 303
    # **コースのトップへ戻す**（#82）。設定画面ではない ── 消した直後に見たいのは
    # 「このコースに何が残っているか」である。
    assert response.headers["location"] == f"/courses/{world.course.id}"

    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)) is None

    # **何がどうなったかを着地点に出す。** 件数の合計では、削除と取り下げが
    # 混ざったときに何が起きたのか言えない（#59）。
    landing = client.get(f"/courses/{world.course.id}").text
    assert "問題セットを片付けました" in landing
    assert "1 件を削除" in landing


# --------------------------------------------------------------------------
# 課題文に貼る画像（#64）
# --------------------------------------------------------------------------


def test_an_uploaded_image_comes_back_with_the_line_to_paste(world: World) -> None:
    """**URL を手で書かせない。** 打ち間違いは「画像が出ない課題文」としてしか
    現れず、なぜ出ないのかが画面から分からない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/images",
        files={"upload": ("shot.png", b"fake png bytes", "image/png")},
        data={"alt": "端末の画面"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "![端末の画面](/images/" in response.text, "貼り付ける 1 行が出ていない"

    # 上げた画像はその場で読める。
    line = response.text.split("![端末の画面](")[1].split(")")[0]
    served = client.get(f"/manage/courses/{world.course.id}/images/{line.rsplit('/', 1)[1]}")
    assert served.status_code == 200
    assert served.content == b"fake png bytes"
    assert served.headers["content-type"].startswith("image/png")


def test_every_console_screen_can_load_a_pasted_image(world: World) -> None:
    """**画面に出る画像は、その画面から取れる**（#111）。

    課題文は `/images/<course>/<name>` を指す。教員側にその経路が無く、
    プレビューも採点画面も TA の課題ページも、画像が全部欠けていた
    （経路は `/manage` 接頭辞の中で宣言されていた）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("shot.png", b"fake png bytes", "image/png")},
        data={"alt": "端末の画面"},
    )
    url = response.json()["markdown"].split("](", 1)[1].split(")", 1)[0]

    # 課題文に貼って保存する。
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": f"## [必須] 題名 ##\n\n![端末の画面]({url})",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    # 編集画面のプレビューに出ている URL が、そのまま取れる。
    editor = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert f'src="{url}"' in editor
    assert client.get(url).status_code == 200

    # **TA も読める。** 課題文の一部なので、読むだけの画面でも要る（#102）。
    ta = world.client("ta")
    assert f'src="{url}"' in ta.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert ta.get(url).status_code == 200


def test_a_learner_cannot_load_a_statement_image_from_the_console(world: World) -> None:
    """採点できないコースの画像は「無い」と答える（提出物と同じ扱い）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("s2400001", Role.LEARNER)
    line = (
        world.client("teacher")
        .post(
            f"/manage/courses/{world.course.id}/images.json",
            files={"upload": ("shot.png", b"fake png bytes", "image/png")},
        )
        .json()["markdown"]
    )
    url = line.split("](", 1)[1].split(")", 1)[0]
    assert world.client("s2400001").get(url).status_code == 404


def test_a_format_that_cannot_be_pasted_is_refused(world: World) -> None:
    """**貼れる形式と提出できる形式は別。** PDF は提出できるが課題文には貼れない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/images",
        files={"upload": ("report.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 400


def test_the_task_editor_takes_the_image_itself(world: World) -> None:
    """**課題の編集画面から上げられる。** コースの設定画面まで往復させると、
    書きかけの問題文が失われる（課題の編集は 1 つのフォームで、保存するまで
    何も残らない）。返すのは貼り付ける 1 行で、差し込みは画面が行う。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 題名 ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    # 編集画面に受け口がある（#64 の欄が課題の編集にも出ている）。
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit")
    assert f"/manage/courses/{world.course.id}/images.json" in page.text
    assert 'data-image-target="#statement"' in page.text
    # **失敗の置き場所を持つ。** 成功の印（緑の ✓）に理由を出すと、貼れなかった
    # ことが「貼れた」に見える（#52 と同じ誤り）。
    assert "data-image-error" in page.text

    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("shot.png", b"fake png bytes", "image/png")},
        data={"alt": "端末の画面"},
    )
    assert response.status_code == 200
    line = response.json()["markdown"]
    assert line.startswith("![端末の画面](/images/")

    # **貼り付ける 1 行が指す URL をそのまま取りに行く**（#111）。以前は
    # ルータの経路（`/manage/courses/.../images/...`）を叩いており、課題文が
    # 指す `/images/...` を誰も返していないことに気づけなかった。
    url = line.split("](", 1)[1].split(")", 1)[0]
    assert url.startswith("/images/"), url
    served = client.get(url)
    assert served.status_code == 200, f"課題文が指す {url} が返らない"
    assert served.content == b"fake png bytes"


def test_a_large_image_is_pasted_at_a_readable_width(world: World) -> None:
    """**縮めずに貼ると写真 1 枚で画面が埋まる。** 課題文の続きが画面外へ出る。

    幅だけを書くので縦横比は保たれる（高さは書かない・`aijudge_authoring.images`）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    # 幅 4000px の PNG（寸法は符号の先頭にある）。
    wide = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (4000).to_bytes(4, "big")
        + (3000).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("shot.png", wide, "image/png")},
    )
    assert response.status_code == 200
    line = response.json()["markdown"]
    assert line.endswith("{width=480}"), line

    # 課題文として描くと幅の付いた画像になる。**高さは付かない。**
    html = render_statement(line)
    assert 'width="480"' in html
    assert "height=" not in html


def test_the_task_editor_refuses_a_format_that_cannot_be_pasted(world: World) -> None:
    """**上げただけで課題は保存しない。** 貼れない形式は理由を返す（#52）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("report.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 400
    assert "課題文に貼れません" in response.json()["detail"]


def test_only_an_instructor_of_the_course_can_upload_an_image(world: World) -> None:
    """画像の受け口も他の /manage と同じ扱い ── 担当教員だけが使える。"""
    world.register("student", Role.LEARNER)
    client = world.client("student")

    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("shot.png", b"fake png bytes", "image/png")},
    )
    assert response.status_code in (401, 403)


# --------------------------------------------------------------------------
# 学習者に出る形のプレビュー（#105）
# --------------------------------------------------------------------------


def test_the_editor_shows_the_statement_as_the_learner_sees_it(world: World) -> None:
    """**欄だけでは、書いたものがどう出るか分からない。** 数式も画像も
    コードの囲みも、学習者アプリを開くまで確かめられなかった。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 題名 ##\n\n本文です\n\n```c\nint main(void){}\n```",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert "学習者に出る形" in page
    # 描画された本文が出ている（Markdown の記号のままではない）。
    assert "<h2>" in page.split("学習者に出る形")[1]
    assert "<code" in page.split("学習者に出る形")[1]


def test_the_preview_is_rendered_by_the_same_function_as_the_learner_page(
    world: World,
) -> None:
    """**描画は 1 つ。** ブラウザで Markdown を描き直すと、普通の文章では
    一致し、間違いが起きるところ（数式・画像の幅）でだけ食い違う。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    draft = "## 題名 ##\n\n$\\sum_{i=1}^{n} i$\n\n![図](/images/x/a.png){width=480}"
    response = client.post(
        f"/manage/courses/{world.course.id}/statement-preview",
        data={"statement": draft},
    )
    assert response.status_code == 200
    assert response.text == render_statement(draft), "学習者と違う描画になっている"
    # 数式はサーバ側で MathML に、画像の幅は属性になる。
    assert "<math" in response.text
    assert 'width="480"' in response.text


def test_the_preview_saves_nothing(world: World) -> None:
    """**描いて返すだけ。** 押した瞬間に版が上がってはいけない（#58）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex04",
            "statement": "## [必須] 題名 ##\n\n元の本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        before = uow.tasks.latest_version(task.id)

    client.post(
        f"/manage/courses/{world.course.id}/statement-preview",
        data={"statement": "## [必須] 題名 ##\n\n書きかけの本文"},
    )
    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(task.id)
    assert after.version == before.version
    assert after.statement == before.statement


def test_only_an_instructor_can_render_a_preview(world: World) -> None:
    """描画口も他の /manage と同じ扱い（TA は課題を読めるが、書きかけは無い）。"""
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/statement-preview", data={"statement": "# x"}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# 教員・TA 自身の提出は成績にも測定にも数えない（#108）
# --------------------------------------------------------------------------


def _trial_submission(world: World, role: Role):
    """その役割で 1 件出したことにする（採点まで積む必要は無い）。"""
    from datetime import UTC, datetime

    from aijudge_core import Artifact, ArtifactKind, ArtifactRole, Submission, SubmissionState
    from aijudge_core.ids import ArtifactId, SubmissionId, new_id

    principal = world.register(f"{role.value}-tester", role)
    _import_example(world)
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        version = uow.tasks.latest_version(task.id)
        now = datetime.now(UTC)
        submission_id = SubmissionId(new_id("sub"))
        submission = Submission(
            id=submission_id,
            task_version_id=version.id,
            learner_id=principal.user_id,
            submitted_as=role,
            state=SubmissionState.SUBMITTED,
            attempt=1,
            artifacts=(
                Artifact(
                    id=ArtifactId(new_id("art")),
                    submission_id=submission_id,
                    role=ArtifactRole.ORIGINAL,
                    kind=ArtifactKind.CODE,
                    filename="main.c",
                    storage_key=f"{submission_id}/main.c",
                    byte_size=1,
                    content_hash="sha256:" + "0" * 64,
                    created_at=now,
                ),
            ),
            created_at=now,
            submitted_at=now,
        )
        uow.submissions.save(submission)
        uow.commit()
    return task, submission


def test_a_trial_submission_is_marked_in_the_list(world: World) -> None:
    """**隠さずに印を付ける。** 数えないが、出したことは事実として残す。"""
    world.register("teacher", Role.INSTRUCTOR)
    _task, submission = _trial_submission(world, Role.INSTRUCTOR)

    page = world.client("teacher").get(f"/courses/{world.course.id}/submissions").text
    assert str(submission.id)[:12] in page or "instructor-tester" in page
    assert "instructorの試行" in page


def test_the_list_filters_by_the_role_at_submission_time(world: World) -> None:
    """絞り込みは**提出時の役割**で行う（いまの受講から引かない・ADR 0013 の轍）。"""
    world.register("teacher", Role.INSTRUCTOR)
    _task, _submission = _trial_submission(world, Role.INSTRUCTOR)
    client = world.client("teacher")

    assert (
        "instructor-tester"
        in client.get(f"/courses/{world.course.id}/submissions?role=instructor").text
    )
    assert (
        "instructor-tester"
        not in client.get(f"/courses/{world.course.id}/submissions?role=learner").text
    )


def test_a_trial_is_not_counted_as_unfinalised(world: World) -> None:
    """**閉じる対象に出さない。** 成績ではないので、いつまでも減らない
    未確定として残り続けてはいけない。
    """
    from aijudge_admin.finalization import pending_counts

    world.register("teacher", Role.INSTRUCTOR)
    task, _submission = _trial_submission(world, Role.INSTRUCTOR)

    # 採点が無いので確定処理の対象にはそもそも入らないが、件数の数え方が
    # 試行を含まないことをここで固定する。
    counts = pending_counts(world.database, world.course.id)
    assert counts.get(task.id, 0) == 0


def test_a_trial_is_never_sampled_for_blind_marking(world: World) -> None:
    """一致度は**学習者の提出に対する**測定である（ADR 0005）。"""
    _task, submission = _trial_submission(world, Role.ASSISTANT)
    console = world.console
    # 抽出率を 100% にしても、試行は選ばれない。
    assert console.blind_sample_rate("cs_intro_c") >= 0.0
    assert console.needs_blind_mark(submission, "cs_intro_c") is False


# --------------------------------------------------------------------------
# 試験の問題セット（#67）
# --------------------------------------------------------------------------


def _exam_unit(world: World, *, starts_at):
    """この問題セットを試験の設定にする（採点開始時刻を先に置く）。"""
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        uow.tasks.save_task(task.model_copy(update={"grading_starts_at": starts_at}))
        uow.commit()
        unit = unit_key(task)
    return task_id, unit


def test_the_unit_page_shows_how_many_submissions_are_waiting(world: World) -> None:
    """**押す前に「何件動くか」を出す。** 押してからでは確認にならない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _task_id, unit = _exam_unit(world, starts_at=datetime.now(UTC) + timedelta(hours=2))

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "採点の待機" in page
    assert "いま待機している提出は 0 件です" in page
    assert "/grade-now" in page
    # 試験の設定であることと、その時刻が教員には見えている。
    assert "この問題セットは" in page and "試験の設定" in page


def test_grading_now_does_not_turn_the_exam_setting_off(world: World) -> None:
    """**何度でも押せる。** 押したあとの提出はまた採点開始時刻まで待つ。

    試験中に「ここまでの提出が採点を通るか」を確かめられ、延長しても勝手に
    始まらない、の両方が要る。
    """
    from aijudge_core.ids import TaskId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id, unit = _exam_unit(world, starts_at=datetime.now(UTC) + timedelta(hours=2))

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/grade-now", follow_redirects=False
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
    assert task.grading_starts_at is not None, "試験の設定が解除された"


def test_a_grading_start_before_submissions_open_is_refused(world: World) -> None:
    """提出が始まる前に「採点を待つ」状態を作らない。

    教員が試験モードだと思っている画面で、提出が即座に採点されることになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _task_id, unit = _exam_unit(world, starts_at=None)

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={
            "opens_at": "2026-09-01T09:00",
            "submissions_open_at": "2026-09-01T10:00",
            "due_at": "2026-09-01T12:00",
            "grading_starts_at": "2026-09-01T09:30",
        },
    )
    assert response.status_code >= 400, "採点開始が提出開始より前でも通った"


# --------------------------------------------------------------------------
# 第 0 回の表示（#86）
# --------------------------------------------------------------------------


def test_the_zeroth_session_is_shown_and_survives_a_save(world: World) -> None:
    """**`0` は「未設定」ではない。**

    模型は 0 を受け付ける（#60）が、画面が `{% if unit.session %}` で畳んで
    いたので第 0 回が消えていた。入力欄は `or` を使っており、**開いて保存し
    直すだけで設定した 0 が失われた**。
    """
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))

    client.post(f"/manage/courses/{world.course.id}/units/{unit}/number", data={"session": "0"})

    # コースのページに出る。
    assert "第 0 回" in client.get(f"/courses/{world.course.id}").text

    # 入力欄が 0 を保持している。**空だと、保存し直したときに消える。**
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'id="session" name="session" type="number" min="0"' in page
    assert 'value="0"' in page

    # 実際に保存し直しても消えない。
    client.post(f"/manage/courses/{world.course.id}/units/{unit}/number", data={"session": "0"})
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).session == 0


def test_a_withdrawn_task_is_visibly_apart_in_the_list(world: World) -> None:
    """**学習者に出ているかどうかは、この画面で最も強い区別である**（#83）。

    提出が来るかどうかがそれで決まる。ピルは他の印（自動テストなし・同じ題名）
    と並ぶので、一覧を上から数えるときには効かない。
    """
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'hidden-from-learners"' not in page
    assert "学習者に出ていません" not in page

    client.post(f"/manage/courses/{world.course.id}/tasks/{task_id}/withdraw")

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "row-link hidden-from-learners" in page, "行が出題中と同じ見た目のまま"
    # 数えるときに気づける形にする。
    assert "うち 1 問は学習者に出ていません" in page


def test_console_rows_open_from_anywhere_but_keep_their_link(world: World) -> None:
    """学習者側（#77）と同じ作法をコンソールにも当てる（#85）。

    **リンクは残す。** 素の HTML に行リンクは無いので、JavaScript が無い環境と
    キーボード操作ではリンクを辿ることになる。**行の中のボタン**（並べ替えの
    ↑↓）はハンドラが避ける ── コンソールではここが学習者側より効く。
    """
    from aijudge_core.ids import TaskId
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert f'data-href="/manage/courses/{world.course.id}/tasks/{task_id}/edit"' in page
    assert f'<a href="/manage/courses/{world.course.id}/tasks/{task_id}/edit">修正</a>' in page


def test_a_fully_withdrawn_set_is_marked_on_the_course_page(world: World) -> None:
    """**セットの中だけでなく、セット自体も分ける**（#83 の追補）。

    #83 は問題セットの中の課題に印を付けたが、コースに並ぶセットの行は
    生きているものと同じ見た目のままだった ── 一覧を上から読んで「この
    コースに何が出ているか」を数えるときに、出ていないセットが混ざる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)

    page = client.get(f"/courses/{world.course.id}").text
    assert "出題していません" not in page

    client.post(f"/manage/courses/{world.course.id}/tasks/{task_id}/withdraw")

    page = client.get(f"/courses/{world.course.id}").text
    assert "出題していません" in page
    assert "hidden-from-learners" in page
