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


def test_an_assistant_cannot_open_a_unit_page(world: World) -> None:
    """締切と一括確定は成績に直接効く。TA には開けない。"""
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").get(f"/manage/courses/{world.course.id}/units/ex01")
    assert response.status_code == 403


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
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "知識要素" in body
    assert "cs" in body
    assert "コースをまたいで共有されます" in body


def test_an_instructor_cannot_create_a_root_component(world: World) -> None:
    """新しい分野の根を作るのは管理者の操作。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc",
        data={"key": "cs.loops", "label": "ループ"},
    )
    assert response.status_code == 400
    assert "第 1 階層" in response.json()["detail"]


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


def test_generation_needs_a_registered_component(world: World) -> None:
    """AI に KC を作らせない。生成は登録済みからの選択だけ。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{_unit_of(world)}/generate",
        data={"key_suffix": "p9", "kc": ["cs.made.up"]},
    )
    assert response.status_code == 400


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


def test_the_candidate_page_takes_text_or_a_file(world: World) -> None:
    """シラバスはブラウザ側で描画されるページで、URL からは本文を取れない。

    だから本文そのものを受け取る ── 貼り付けか、PDF の添付。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc/candidates").text
    assert 'name="text"' in body
    assert 'type="file"' in body


def test_an_unreadable_attachment_says_why(world: World) -> None:
    """スキャン画像の PDF を黙って OCR に流さない。読めないとそう言う。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/candidates",
        files={"upload": ("syllabus.pdf", b"not really a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert "本文を取り出せませんでした" in response.json()["detail"]


def test_an_unsupported_attachment_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/candidates",
        files={"upload": ("syllabus.xlsx", b"binary", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "読めません" in response.json()["detail"]


def test_a_short_paste_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/candidates", data={"text": "短い"}
    )
    assert response.status_code == 400


def test_adopting_a_candidate_follows_the_same_rules(world: World) -> None:
    """候補だからといって規則は緩めない（名前空間・親の実在・第 1 階層）。"""
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/adopt",
        data={"kc": ["cs.loops.termination"], "label": ["停止条件"]},
    )
    assert response.status_code == 400
    assert "親" in response.json()["detail"]


def test_the_course_settings_link_to_both_flows(world: World) -> None:
    """基本情報と知識要素は別の作業。入口も分ける。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    settings = client.get(f"/manage/courses/{world.course.id}").text
    assert f"/manage/courses/{world.course.id}/basics" in settings
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
    assert 'class="reading flash" hidden' in body
    assert "[hidden]{display:none!important}" in body


# --------------------------------------------------------------------------
# 受講者（別ページ）
# --------------------------------------------------------------------------


def test_the_enrolments_have_their_own_page(world: World) -> None:
    """受講 100 名規模。設定を 1 つ直しに来た教員に 100 行めくらせない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    settings = client.get(f"/manage/courses/{world.course.id}").text
    assert f"/manage/courses/{world.course.id}/enrolments" in settings

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

    for url in (
        f"/manage/courses/{world.course.id}",
        f"/manage/courses/{world.course.id}/enrolments",
    ):
        page = client.get(url).text
        rows = page[page.index("rolecounts") :]
        assert ">2</b>" in rows, url  # learner
        assert ">1</b>" in rows, url  # instructor
        assert ">0</b>" in rows, url  # assistant / admin は 0 名でも並ぶ
        assert "assistant" in rows, url


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
    assert "この設定で試す" in body
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
        "criterion_levels": [levels for _c, _t, _w, levels in rows],
    }


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

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/rubric/reset",
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert [c.code for c in latest.criteria] == ["correctness", "style"]
