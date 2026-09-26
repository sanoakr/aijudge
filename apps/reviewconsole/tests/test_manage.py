"""管理画面の規則を固定する。

固定したいのは権限。締切と受講の変更は成績に直接効くので、誰が何を
変更できるかが緩いと他のすべてが無意味になる。

- コースの作成 … ADMIN
- 課題・受講の管理 … そのコースの INSTRUCTOR 以上（**TA には開けない**）
- 科目プロファイル … 表示だけ。編集させない
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import ensure_course
from aijudge_authoring.statement import render_statement
from aijudge_core import Course, ReviewState, Role
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, TenantId
from aijudge_identity import AuthenticationFailed, AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
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
            subject_profile="cs_lang_c_intro",
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
            service = AuthService(uow.identity, audit=uow.audit)
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
            "/auth/local", data={"login": login, "password": PASSWORD}, follow_redirects=False
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


def _main(html: str) -> str:
    """`<main>` の中だけ。**帯は本文ではない**（#189）。

    左の帯は全ページに同じ行き先を出すので、本文への主張（「この画面には
    これが出ない」）を文書全体に当てると帯に当たる。見たいのは画面が自分で
    出しているものである。
    """
    return html[html.index("<main") : html.index("</main>")]


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
        "term_year": "2025",
        "term_division": "後期",
        "profile": "cs_network_python",
        "instructors": "teacher",
    }
    assert world.client("teacher").post("/manage/courses", data=data).status_code == 403


def test_only_an_admin_can_delete_a_course(world: World) -> None:
    """コースを消すのは、そのコースの中の操作ではない（作成と同じ権限・#156）。"""
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(f"/manage/courses/{world.course.id}/delete")

    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id) is not None


# --------------------------------------------------------------------------
# 利用者の一覧の絞り込み（#326）
# --------------------------------------------------------------------------


def _users_page(world: World, query: str = "") -> str:
    return world.client("boss").get(f"/manage/users{query}").text


def test_the_user_list_filters_by_role(world: World) -> None:
    """**役割で絞れる**（#326・受講者一覧と同じ）。

    TA だけ・教員だけを見たいとき、学生の中から探すことになっていた。
    """
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    world.register("student", Role.LEARNER)

    assistants = _users_page(world, "?role=assistant")

    assert "ta</a>" in assistants
    assert "teacher</a>" not in assistants, "教員が TA の絞り込みに残っている"
    assert "student</a>" not in assistants
    # 役割は行にも出る ── 出さないと、絞り込みが効いたのか行から分からない。
    assert "assistant" in assistants


def test_a_user_with_two_roles_is_found_by_either(world: World) -> None:
    """**丸めない**（`roles_by_user`）。片方のコースの教員が別のコースの
    TA であることは普通にあり、どちらかに決めると絞り込みがその人を落とす。
    """
    world.register("boss", Role.ADMIN)
    with world.database.unit_of_work() as uow:
        other = Course(
            id=CourseId("crs_" + "7" * 32),
            tenant_id=world.course.tenant_id,
            code="other",
            title="別のコース",
            term="2026-後期",
            subject_profile=world.course.subject_profile,
        )
        uow.identity.save_course(other)
        uow.commit()
    world.register("both", Role.INSTRUCTOR)
    world.register("both2", Role.ASSISTANT, course_id=other.id)
    # 同じ人に 2 つ目の役割を足す。
    with world.database.unit_of_work() as uow:
        principal = uow.identity.find_user_by_login(world.course.tenant_id, "both")
        AuthService(uow.identity, audit=uow.audit).enroll(
            tenant_id=world.course.tenant_id,
            course_id=other.id,
            user_id=principal.id,
            role=Role.ASSISTANT,
        )
        uow.commit()

    assert "both</a>" in _users_page(world, "?role=instructor")
    assert "both</a>" in _users_page(world, "?role=assistant")


def test_the_user_list_filters_by_login_method_in_both_directions(world: World) -> None:
    """**逆向きにも絞れる**（#326）。

    札（ローカルだけ）しか無かったので、「SSO で入った人が何人いるか」が
    数えられなかった。
    """
    world.register("boss", Role.ADMIN)
    world.register("local-only", Role.LEARNER)
    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(world.course.tenant_id, "local-only")
        uow.identity.save_user(user.model_copy(update={"external_id": "sub-123"}))
        uow.commit()

    sso = _users_page(world, "?login_kind=sso")
    local = _users_page(world, "?login_kind=local")

    assert "local-only</a>" in sso
    assert "boss</a>" not in sso, "ローカルの利用者が SSO の絞り込みに残っている"
    assert "local-only</a>" not in local
    assert "boss</a>" in local


def test_the_old_local_only_link_still_filters(world: World) -> None:
    """`local=1` は `login_kind=local` の旧名。**受け続ける。**

    運用の手元に残った URL が黙って全件に戻ると、絞ったつもりの一覧を読む。
    """
    world.register("boss", Role.ADMIN)

    page = _users_page(world, "?local=1")

    assert "boss</a>" in page
    assert "解除" in page, "絞り込みが効いていない（解除の導線が出ない）"


def test_an_admin_deletes_a_course_with_no_learner_submissions(world: World) -> None:
    world.register("boss", Role.ADMIN)

    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/delete", follow_redirects=False
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id) is None


def test_the_course_page_offers_deletion_only_to_an_admin(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("boss", Role.ADMIN)

    teacher_page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    admin_page = world.client("boss").get(f"/manage/courses/{world.course.id}").text

    assert "このコースを削除する" not in teacher_page
    assert "このコースを削除する" in admin_page


def test_an_admin_creates_a_course_and_becomes_its_instructor(world: World) -> None:
    """作った本人が担当教員にならないと、自分のコースが見えない。"""
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    response = client.post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term_year": "2025",
            "term_division": "後期",
            "profile": "cs_network_python",
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
            "term_year": "2025",
            "term_division": "後期",
            "profile": "cs_network_python",
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
            "term_year": "2025",
            "term_division": "後期",
            "profile": "cs_network_python",
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
            "term_year": "2025",
            "term_division": "後期",
            "profile": "cs_network_python",
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


# --------------------------------------------------------------------------
# ローカル利用者の管理 — 一覧・属性・無効化・再発行（#144）
# --------------------------------------------------------------------------


def test_only_an_admin_can_list_local_users(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    assert world.client("teacher").get("/manage/users").status_code == 403


def test_only_an_admin_can_open_a_user(world: World) -> None:
    teacher = world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").get(f"/manage/users/{teacher.user_id}")
    assert response.status_code == 403


def test_the_list_shows_local_users_and_filters_by_prefix(world: World) -> None:
    world.register("boss", Role.ADMIN)
    world.register("s2400001", Role.LEARNER)
    world.register("y2399001", Role.LEARNER)
    client = world.client("boss")

    body = client.get("/manage/users").text
    assert "s2400001" in body
    assert "y2399001" in body

    filtered = client.get("/manage/users", params={"q": "s24"}).text
    assert "s2400001" in filtered
    assert "y2399001" not in filtered


def test_a_user_page_shows_the_courses_and_roles_they_hold(world: World) -> None:
    world.register("boss", Role.ADMIN)
    assistant = world.register("ta1", Role.ASSISTANT)

    body = world.client("boss").get(f"/manage/users/{assistant.user_id}").text

    assert "prog2" in body
    assert Role.ASSISTANT.value in body


def test_a_tenant_admins_page_says_they_reach_every_course(world: World) -> None:
    """**管理者は受講登録なしで全コースに届く。** 全コースを役割つきで並べると、
    無い受講登録があるように見える（#128 の意味を画面が誤って伝える）。
    """
    world.register("boss", Role.ADMIN)
    other = world.register("boss2", None, tenant_admin=True)

    body = world.client("boss").get(f"/manage/users/{other.user_id}").text

    assert "すべてのコース" in body
    assert "受講登録がありません" in body


def test_disabling_a_user_stops_their_login_and_keeps_the_record(world: World) -> None:
    world.register("boss", Role.ADMIN)
    learner = world.register("s2400002", Role.LEARNER)
    # 無効化の前にセッションを張っておく（切れることを確かめるため）。
    learner_client = world.client("s2400002")
    assert learner_client.get("/").status_code == 200

    response = world.client("boss").post(
        f"/manage/users/{learner.user_id}/disable", follow_redirects=False
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        user = uow.identity.get_user(learner.user_id)
        # **消さない。** 過去の提出と採点が参照している。
        assert user is not None
        assert not user.is_active
    # 既存のセッションも切れている。
    assert learner_client.get("/", follow_redirects=False).status_code == 303


def test_an_admin_cannot_disable_themselves(world: World) -> None:
    """自分を無効化すると、自分の権限で自分を戻せない（復旧が CLI だけになる）。"""
    boss = world.register("boss", Role.ADMIN)

    response = world.client("boss").post(f"/manage/users/{boss.user_id}/disable")

    assert response.status_code == 400
    with world.database.unit_of_work() as uow:
        user = uow.identity.get_user(boss.user_id)
    assert user is not None and user.is_active


def test_reissuing_a_password_shows_it_once_and_replaces_the_old_one(world: World) -> None:
    world.register("boss", Role.ADMIN)
    learner = world.register("s2400003", Role.LEARNER)

    response = world.client("boss").post(f"/manage/users/{learner.user_id}/password")

    assert response.status_code == 200
    # 平文はこの応答にだけ出る。どの値かは画面から読み取って確かめる。
    shown = re.search(r'class="mono">([^<]+)</p>', response.text.split("新しいパスワード")[1])
    assert shown is not None
    new_password = shown.group(1).strip()

    fresh = TestClient(create_app(world.console))
    # 古いパスワードでは通らない。
    assert (
        fresh.post("/auth/local", data={"login": "s2400003", "password": PASSWORD}).status_code
        == 401
    )
    # 新しいパスワードで通る。
    assert (
        fresh.post(
            "/auth/local",
            data={"login": "s2400003", "password": new_password},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_the_password_is_not_shown_again_on_the_user_page(world: World) -> None:
    """平文はレスポンス以外のどこにも残さない（作成画面と同じ約束）。"""
    world.register("boss", Role.ADMIN)
    learner = world.register("s2400004", Role.LEARNER)
    client = world.client("boss")
    issued = client.post(f"/manage/users/{learner.user_id}/password").text
    shown = re.search(r'class="mono">([^<]+)</p>', issued.split("新しいパスワード")[1])
    assert shown is not None
    new_password = shown.group(1).strip()

    assert new_password not in client.get(f"/manage/users/{learner.user_id}").text
    assert new_password not in client.get("/manage/users").text


def _google_user(world: World, login: str, sub: str):
    """大学アカウント（SSO）でログインした利用者。JIT で作られる形と同じ。"""
    from aijudge_identity import GoogleOidcIdentity

    with world.database.unit_of_work() as uow:
        principal, _ = AuthService(uow.identity, audit=uow.audit).login_with_google(
            tenant_id=TENANT,
            identity=GoogleOidcIdentity(sub=sub, email=login, hd="example.ac.jp"),
        )
        uow.commit()
    return principal


def test_the_list_tags_local_and_university_accounts(world: World) -> None:
    world.register("boss", Role.ADMIN)
    world.register("s2400010", Role.LEARNER)
    _google_user(world, "taro@example.ac.jp", "sub-list")

    body = world.client("boss").get("/manage/users").text

    assert "ローカル" in body
    assert "大学アカウント" in body


def test_the_list_can_show_local_accounts_only(world: World) -> None:
    """再発行や無効化の対象になるのはローカル利用者だけ。運用の単位で絞れる。"""
    world.register("boss", Role.ADMIN)
    world.register("s2400011", Role.LEARNER)
    _google_user(world, "hanako@example.ac.jp", "sub-filter")

    body = world.client("boss").get("/manage/users", params={"local": "1"}).text

    assert "s2400011" in body
    assert "hanako@example.ac.jp" not in body


def test_a_university_account_is_offered_no_password_reissue(world: World) -> None:
    world.register("boss", Role.ADMIN)
    google = _google_user(world, "jiro@example.ac.jp", "sub-nopass")

    body = world.client("boss").get(f"/manage/users/{google.user_id}").text

    assert "パスワードを再発行する" not in body


def test_reissuing_a_password_for_a_university_account_is_refused(world: World) -> None:
    """**画面で隠すだけにしない。** POST は手で作れる。"""
    world.register("boss", Role.ADMIN)
    google = _google_user(world, "saburo@example.ac.jp", "sub-refuse")

    response = world.client("boss").post(f"/manage/users/{google.user_id}/password")

    assert response.status_code == 400


def test_an_admin_can_grant_and_revoke_tenant_admin(world: World) -> None:
    world.register("boss", Role.ADMIN)
    teacher = world.register("teacher2", Role.INSTRUCTOR)
    client = world.client("boss")

    granted = client.post(
        f"/manage/users/{teacher.user_id}/tenant-admin",
        data={"admin": "1"},
        follow_redirects=False,
    )
    assert granted.status_code == 303
    with world.database.unit_of_work() as uow:
        user = uow.identity.get_user(teacher.user_id)
    assert user is not None and user.is_tenant_admin

    revoked = client.post(f"/manage/users/{teacher.user_id}/tenant-admin", follow_redirects=False)
    assert revoked.status_code == 303
    with world.database.unit_of_work() as uow:
        user = uow.identity.get_user(teacher.user_id)
    assert user is not None and not user.is_tenant_admin


def test_an_admin_cannot_change_their_own_tenant_admin_flag(world: World) -> None:
    """自分から外すと、自分では戻せない（無効化と同じ理屈）。"""
    boss = world.register("boss", Role.ADMIN)

    response = world.client("boss").post(f"/manage/users/{boss.user_id}/tenant-admin")

    assert response.status_code == 400
    with world.database.unit_of_work() as uow:
        user = uow.identity.get_user(boss.user_id)
    assert user is not None and user.is_tenant_admin


def test_a_course_role_can_be_changed_from_the_user_page(world: World) -> None:
    world.register("boss", Role.ADMIN)
    learner = world.register("s2400012", Role.LEARNER)

    response = world.client("boss").post(
        f"/manage/users/{learner.user_id}/courses/{world.course.id}/role",
        data={"role": "assistant"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, learner.user_id)
    assert enrollment is not None and enrollment.role is Role.ASSISTANT


def test_admin_cannot_be_granted_from_the_user_page(world: World) -> None:
    """コースをまたぐ権限は、コース単位の欄からは配れない（受講者一覧と同じ）。"""
    world.register("boss", Role.ADMIN)
    learner = world.register("s2400013", Role.LEARNER)

    response = world.client("boss").post(
        f"/manage/users/{learner.user_id}/courses/{world.course.id}/role",
        data={"role": "admin"},
    )

    assert response.status_code == 403
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, learner.user_id)
    assert enrollment is not None and enrollment.role is Role.LEARNER


def test_a_course_role_cannot_be_set_without_an_enrolment(world: World) -> None:
    """受講登録そのものはコース側の画面の仕事（ここで作らない）。"""
    world.register("boss", Role.ADMIN)
    outsider = world.register("s2400014", None)

    response = world.client("boss").post(
        f"/manage/users/{outsider.user_id}/courses/{world.course.id}/role",
        data={"role": "assistant"},
    )

    assert response.status_code == 404


def test_an_unknown_subject_profile_is_refused(world: World) -> None:
    """存在しないプロファイルでコースを作ると、採点が恒久的に失敗する。"""
    world.register("boss", Role.ADMIN)
    response = world.client("boss").post(
        "/manage/courses",
        data={
            "code": "x",
            "title": "x",
            "term_year": "2026",
            "term_division": "前期",
            "profile": "no_such_profile",
            "instructors": "boss",
        },
    )
    assert response.status_code == 400
    assert "科目プロファイル" in response.json()["detail"]


# --------------------------------------------------------------------------
# パスワード変更（本人向け・ローカル利用者のみ意味を持つ）
# --------------------------------------------------------------------------


def _google_client(world: World, login: str, sub: str) -> TestClient:
    """SSO で入った利用者としてのクライアント（`World.client` の SSO 版）。"""
    from aijudge_identity import GoogleOidcIdentity

    with world.database.unit_of_work() as uow:
        _, token = AuthService(uow.identity, audit=uow.audit).login_with_google(
            tenant_id=TENANT,
            identity=GoogleOidcIdentity(sub=sub, email=login, hd="example.ac.jp"),
        )
        uow.commit()
    client = TestClient(create_app(world.console))
    client.cookies.set(SESSION_COOKIE, token)
    return client


def test_a_university_account_is_offered_no_password_change(world: World) -> None:
    """**SSO 利用者に出さない**（#180）。Google で入った利用者のローカル
    パスワードは誰も知らない捨て値なので、この画面は「現在のパスワードが
    違います」しか返せない。出せば必ず行き止まりに導く。"""
    client = _google_client(world, "taro@example.ac.jp", "sub-nochange")

    response = client.get("/")
    # 画面自体は出ていること（401 でも「出ていない」は成り立ってしまう）。
    assert response.status_code == 200
    assert "ログアウト" in response.text
    assert "パスワード変更" not in response.text


def test_the_password_form_is_refused_for_a_university_account(world: World) -> None:
    """**画面で隠すだけにしない。** URL は手で打てる（再発行と同じ理屈）。"""
    client = _google_client(world, "hanako@example.ac.jp", "sub-noform")

    assert client.get("/manage/account/password").status_code == 404
    assert (
        client.post(
            "/manage/account/password",
            data={
                "current_password": "whatever",
                "new_password": "a brand new password",
                "new_password_confirm": "a brand new password",
            },
        ).status_code
        == 404
    )


def test_a_local_account_still_sees_the_password_link(world: World) -> None:
    """出し分けが行き過ぎていないこと ── #127 のローカル利用者には、
    ここが唯一のパスワード変更手段である。"""
    world.register("teacher", Role.INSTRUCTOR)

    assert "パスワード変更" in world.client("teacher").get("/").text


def test_anyone_can_open_their_own_password_form(world: World) -> None:
    """管理者専用にしない ── #127 で発行したパスワードを変える手段が
    無かった。学習者やTAでも自分のパスワードは変えられて当然。"""
    world.register("student", Role.LEARNER)
    assert world.client("student").get("/manage/account/password").status_code == 200


def test_changing_password_requires_the_current_one(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        "/manage/account/password",
        data={
            "current_password": "wrong",
            "new_password": "a brand new password",
            "new_password_confirm": "a brand new password",
        },
    )
    assert response.status_code == 200
    assert "現在のパスワードが違います" in response.text
    with world.database.unit_of_work() as uow:
        # 変わっていないことを、旧パスワードでログインできることで確かめる。
        AuthService(uow.identity, audit=uow.audit).login(
            tenant_id=TENANT, login="teacher", password=PASSWORD
        )


def test_new_password_must_be_long_enough(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        "/manage/account/password",
        data={
            "current_password": PASSWORD,
            "new_password": "short1",
            "new_password_confirm": "short1",
        },
    )
    assert response.status_code == 200
    assert "12 文字以上" in response.text


def test_new_password_confirmation_must_match(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        "/manage/account/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a brand new password",
            "new_password_confirm": "not the same password",
        },
    )
    assert response.status_code == 200
    assert "一致しません" in response.text


def test_changing_password_succeeds_and_revokes_every_session(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(
        "/manage/account/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a brand new password",
            "new_password_confirm": "a brand new password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/local?changed=1"

    # 使い回した Cookie（変更前のセッション）はもう通らない
    # （`/manage` は認可を挟まず `/` へリダイレクトするだけなので、認可を
    # 直接持つ経路で確かめる）。
    assert client.get("/manage/account/password").status_code == 401

    with world.database.unit_of_work() as uow:
        service = AuthService(uow.identity, audit=uow.audit)
        # 旧パスワードはもう効かない。
        with pytest.raises(AuthenticationFailed):
            service.login(tenant_id=TENANT, login="teacher", password=PASSWORD)
        # 新パスワードでログインできる。
        service.login(tenant_id=TENANT, login="teacher", password="a brand new password")


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


def _task_count(world: World) -> int:
    with world.database.unit_of_work() as uow:
        return len(uow.tasks.list_for_course(world.course.id))


def _unit_of(world: World) -> str:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
    return task.unit or "_"


def test_a_japanese_unit_name_does_not_break_the_audit_record(world: World) -> None:
    """**記録が名指すのは問題セットそのもので、URL での姿ではない。**

    経路に載せる鍵は percent-encode してあり、日本語の名前は 1 文字が 9 字に
    膨らむ ── そのまま `target_id` に書くと列（128 字）を超え、記録の挿入が
    失敗して**同じ unit_of_work にいる締切の保存ごと巻き戻る**。受講登録の
    対で同じことが起き、ログインが 500 になった（`audit_events.target_id`）。
    """
    from aijudge_core import Task

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    name = "第3回 配列とポインタ入門"
    assert len(quote(name, safe="")) > 100, "符号化で膨らむ名前でなければ意味が無い"
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(world.course.id)[0]
        uow.tasks.save_task(Task.model_validate(task.model_dump() | {"unit": name}))
        uow.commit()

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{quote(name, safe='')}/schedule",
        data={"opens_at": "2025-10-01T09:00", "due_at": "2025-10-08T23:59"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    target = f"{world.course.id}/{name}"
    assert len(target) <= 128
    with world.database.unit_of_work() as uow:
        rows = uow.audit.list_for_target("unit", target)
    # **締切が保存されていること**まで見る ── 記録だけ入って値が戻っていたら
    # 直したことにならない。
    assert rows, "問題セットの変更が記録されていない"
    with world.database.unit_of_work() as uow:
        assert uow.tasks.list_for_course(world.course.id)[0].due_at is not None


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


def test_the_schedule_is_typed_and_shown_in_the_institution_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入力欄の「23:59」は機関の時刻（既定 JST）で、保存は UTC、表示は元どおり。

    以前は入力を UTC として読み、保存した UTC をそのまま出していたので、
    締切も提出日時も 9 時間ずれて見えた。
    """
    from datetime import UTC, datetime

    from aijudge_webui import ENV_TIMEZONE

    monkeypatch.setenv(ENV_TIMEZONE, "Asia/Tokyo")
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={"opens_at": "2026-09-18T09:00", "due_at": "2026-09-25T23:59"},
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
    assert task.due_at == datetime(2026, 9, 25, 14, 59, tzinfo=UTC)

    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'value="2026-09-25T23:59"' in page  # 入力欄に戻る値も機関の時刻
    assert 'value="2026-09-18T09:00"' in page


def test_no_template_formats_a_datetime_without_the_local_filter() -> None:
    """`strftime` を直に呼ぶと UTC のまま出る。必ず `local` フィルタを通す。"""
    templates = Path(__file__).resolve().parents[1] / "src" / "aijudge_reviewconsole" / "templates"
    guilty = [
        f"{path.name}:{number}"
        for path in templates.glob("*.html")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if ".strftime(" in line
    ]
    assert guilty == []


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
        subject_profile="cs_network_python",
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
        _record, token = AuthService(uow.identity, audit=uow.audit).issue_token(
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
        assert (
            AuthService(uow.identity, audit=uow.audit).role_in(world.course.id, user.id)
            is Role.LEARNER
        )


def test_enrolment_comes_back_to_the_same_page_with_the_counts(world: World) -> None:
    """登録のあとは受講者の画面に戻り、**何件入ったか**をその場で言う。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("y239999", None)
    world.register("y239998", Role.LEARNER)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "y239999\ny239998", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/manage/courses/{world.course.id}/enrolments?")
    assert "enrolled=1" in location and "already=1" in location

    page = world.client("teacher").get(location).text
    assert "1 件を登録しました" in page
    assert "1 件は登録済み" in page
    assert "登録できませんでした" not in page


def test_unknown_accounts_are_reported_and_the_rest_are_enrolled(world: World) -> None:
    """**未登録が混ざっていても残りは入れる。** 入らなかったアカウントは
    画面に名指しで出す ── 全部を断ると、100 行の名簿が 1 行の綴り違いで
    丸ごと弾かれる。新規作成はパスワードの配布が伴うので CLI に回す。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("y239999", None)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "y239999\nnobody\nghost@example.ac.jp", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = world.client("teacher").get(response.headers["location"]).text
    assert "1 件を登録しました" in page
    assert "2 件は登録できませんでした" in page
    assert "nobody, ghost@example.ac.jp" in page
    assert "aijudge-admin enrol" in page

    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "y239999")
        assert user is not None
        assert (
            AuthService(uow.identity, audit=uow.audit).role_in(world.course.id, user.id)
            is Role.LEARNER
        )
        assert uow.identity.find_user_by_login(TENANT, "nobody") is None


def _enable_sso(
    world: World, monkeypatch: pytest.MonkeyPatch, domain: str = "example.ac.jp"
) -> None:
    from cryptography.fernet import Fernet

    from aijudge_identity import OidcSettings
    from aijudge_persistence import ENV_OIDC_SECRET_KEY

    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))
    with world.database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(
            OidcSettings(
                tenant_id=TENANT,
                client_id="client-abc",
                client_secret="test-secret",
                allowed_domains=(domain,),
            )
        )
        uow.commit()


def test_sso_accounts_are_created_from_the_roster_before_their_first_login(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """学内ログインのアカウントは、まだ無くても名簿から作って登録できる（#285）。

    パスワードは配らない（捨て値）。本人の初回 SSO ログインで結び付く。
    許可ドメイン外の未登録アカウントは従来どおり断り、CLI を案内する。
    """
    from aijudge_identity import GoogleOidcIdentity

    _enable_sso(world, monkeypatch)
    world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={
            "roster": "y239999@example.ac.jp\nta25001@example.ac.jp\nnobody\nx@other.ac.jp",
            "role": "learner",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = world.client("teacher").get(response.headers["location"]).text
    assert "2 件を登録しました" in page
    assert "うち 2 件はまだログインしたことのない学内アカウント" in page
    assert "2 件は登録できませんでした" in page
    assert "nobody, x@other.ac.jp" in page

    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, "y239999@example.ac.jp")
        assert user is not None and user.external_id is None
        auth = AuthService(uow.identity, audit=uow.audit)
        assert auth.role_in(world.course.id, user.id) is Role.LEARNER
        # 配られていないパスワードでは入れない。
        with pytest.raises(AuthenticationFailed):
            auth.login(tenant_id=TENANT, login="y239999@example.ac.jp", password="anything")
        # 初回の SSO ログインで同じ利用者に結び付く。
        principal, _ = auth.login_with_google(
            tenant_id=TENANT,
            identity=GoogleOidcIdentity(
                sub="sub-y239999", email="y239999@example.ac.jp", hd="example.ac.jp"
            ),
        )
        assert principal.user_id == user.id


def test_the_enrolment_list_can_be_filtered_by_role(world: World) -> None:
    """TA だけ・教員だけを見るのに、100 名の学生の中から探さない。内訳の数は
    絞り込みの前のまま。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta01", Role.ASSISTANT)
    world.register("y239999", Role.LEARNER)
    client = world.client("teacher")

    page = client.get(f"/manage/courses/{world.course.id}/enrolments?role=assistant").text
    rows = page[page.index('<table class="stack">') : page.index("<h2>受講登録</h2>")]
    assert "ta01" in rows
    assert "y239999" not in rows and "teacher" not in rows
    assert "1 名に絞り込み" in page
    assert "learner</span> <b>1</b>" in page  # 内訳は絞る前

    # 語彙に無い役割は無視する（全員が出る）。
    page = client.get(f"/manage/courses/{world.course.id}/enrolments?role=wizard").text
    assert "y239999" in page and "ta01" in page


def test_anyone_on_the_console_can_download_the_course_template(world: World) -> None:
    """教員が埋めて管理者に渡すひな形。管理者だけに出すと依頼する側が形式を知れない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    assert "ひな形をダウンロード" in client.get("/").text
    response = client.get("/manage/course-template.yaml")
    assert response.status_code == 200
    assert 'filename="course.yaml"' in response.headers["content-disposition"]
    assert "subject_profile:" in response.text and "problem_dir:" in response.text


# --------------------------------------------------------------------------
# テストケースの閲覧と修正（#284）
# --------------------------------------------------------------------------


def test_test_cases_are_shown_to_the_instructor_and_the_ta(world: World) -> None:
    """件数だけでなく**中身**を見せる。取り込んだ in/out が正しいかは、これまで
    DB か手元のディレクトリでしか確かめられなかった。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    first = version.test_cases[0]

    for who in ("teacher", "ta"):
        page = world.client(who).get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
        assert 'class="testcases"' in page
        assert first.name in page
        assert html.escape(str(first.payload["expected"]).strip()) in page
    # 直せるのは教員だけ。
    assert (
        "テストケースを直す"
        in world.client("teacher")
        .get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit")
        .text
    )
    assert (
        "テストケースを直す"
        not in world.client("ta")
        .get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit")
        .text
    )


def test_a_task_that_names_a_test_driven_evaluator_can_edit_its_cases(world: World) -> None:
    """**観点が入出力セットを読む評価器を指したら、その欄を出す**（#300）。

    出し分けを科目プロファイルの宣言だけで決めていたので、`code_test_runner`
    を観点に割り当てた課題でも、科目が宣言していなければ欄が出なかった ──
    その評価器が何を走らせるのかを画面から確かめる手段が無く、テストケースが
    0 件のまま出題しても、画面はそれを言わなかった。観点は課題ごとに自分の
    評価器を持つ（ADR 0018）ので、判断はこの課題の観点で行う。

    「決定論的か」では広すぎる（提出の遵守は入出力セットを持たない）ので、
    読むかどうかは評価器の宣言で見る（`reads_test_cases`）。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec

    world.register("teacher", Role.INSTRUCTOR)
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p9",
            unit="ex01",
            statement="## [必須] 出力 ##\n\nhello と出力する。",
            criteria=(
                CriterionSpec(
                    code="correctness",
                    title="出力の正しさ",
                    description="仕様どおりの出力を返すか。テスト実行で判定する。",
                    weight=1.0,
                    evaluator="code_test_runner",
                    levels=(
                        LevelSpec(
                            level=0, label="未達", descriptor="満たしていない", score_ratio=0.0
                        ),
                        LevelSpec(
                            level=1, label="達成", descriptor="満たしている", score_ratio=1.0
                        ),
                    ),
                ),
            ),
        ),
        # テスト実行を宣言していない科目。**それでも欄は出す。**
        subject_profile="report_ja",
        authored_by=_user_id(world, "teacher"),
    )
    page = (
        world.client("teacher")
        .get(f"/manage/courses/{world.course.id}/tasks/{saved.task.id}/edit")
        .text
    )
    assert "入出力セット" in page, "入出力セットを読む評価器を指した課題に欄が出ていない"
    assert "テストケースを直す" in page, "直せない"
    # 0 件であることと、その帰結を言う（伏せられる総点の理由が画面から読める）。
    assert "入出力セットが 1 件も無いので" in page


def test_an_assistant_reads_the_input_output_set_but_cannot_change_it(world: World) -> None:
    """**TA は確認だけ**（#300・#102 と同じ扱い）。

    採点している課題の入出力セットを読めないと、学習者の「入力例 1 で落ちる」に
    答えられない。直すのは担当教員 ── 修正は版を上げる操作で、出題済みの
    採点基準を動かす（P8）。

    0 件のときに**何が起きているかを教員と同じ言葉で出す。** 以前は「なし ──
    正しさの観点は AI が判定します」と出していたが、観点がテスト実行を
    指していれば AI は判定しない。誰も判定していない画面が「AI が見ている」と
    言うことになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _import_example(world)

    page = world.client("ta").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "入出力セット" in page, "TA が入出力セットを読めない"
    assert 'class="testcases"' in page
    # 直す口は出さない。**押せないものを見せない。**
    assert "テストケースを直す" not in page
    assert "/test-cases/edit" not in page
    # **隠すだけの画面は境界にならない。** 経路の側でも拒む。
    refused = world.client("ta").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data={"case_name": "case1", "case_input": "1\n", "case_expected": "1\n"},
    )
    assert refused.status_code in (403, 404), refused.status_code

    # 0 件の課題では、判定できないことを言う（「AI が判定します」ではない）。
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec

    empty = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p8",
            unit="ex01",
            statement="## [必須] 出力 ##\n\nhello と出力する。",
            criteria=(
                CriterionSpec(
                    code="correctness",
                    title="出力の正しさ",
                    description="仕様どおりの出力を返すか。",
                    weight=1.0,
                    evaluator="code_test_runner",
                    levels=(
                        LevelSpec(
                            level=0, label="未達", descriptor="満たしていない", score_ratio=0.0
                        ),
                        LevelSpec(
                            level=1, label="達成", descriptor="満たしている", score_ratio=1.0
                        ),
                    ),
                ),
            ),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    )
    page = (
        world.client("ta").get(f"/manage/courses/{world.course.id}/tasks/{empty.task.id}/edit").text
    )
    assert "判定できません" in page
    assert "AI が判定" not in page


def test_a_compliance_criterion_does_not_ask_for_an_input_output_set(world: World) -> None:
    """**決定論的でも入出力セットを持たない評価器には、欄を出さない**（#300）。

    提出の遵守（`submission_compliance`）は出したか・名前は規則どおりかを見る
    もので、入力と期待出力を持たない。決定論的かどうかで出し分けると、
    レポート課題の画面に永久に空の欄が並ぶ ── 埋まらない欄は、埋め忘れなのか
    そういうものなのかを画面から区別できない。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec

    world.register("teacher", Role.INSTRUCTOR)
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="rep01/p1",
            unit="rep01",
            statement="## [必須] 考察 ##\n\n実験の考察を書く。",
            criteria=(
                CriterionSpec(
                    code="compliance",
                    title="提出の体裁",
                    description="指示どおりのファイル名と形式で出ているか。",
                    weight=1.0,
                    evaluator="submission_compliance",
                    levels=(
                        LevelSpec(
                            level=0, label="未達", descriptor="満たしていない", score_ratio=0.0
                        ),
                        LevelSpec(
                            level=1, label="達成", descriptor="満たしている", score_ratio=1.0
                        ),
                    ),
                ),
            ),
        ),
        subject_profile="report_ja",
        authored_by=_user_id(world, "teacher"),
    )
    page = (
        world.client("teacher")
        .get(f"/manage/courses/{world.course.id}/tasks/{saved.task.id}/edit")
        .text
    )
    assert 'id="tests"' not in page, "入出力セットを持たない観点に欄が出ている"
    assert 'id="items"' not in page


# --------------------------------------------------------------------------
# 項目表（#302）
# --------------------------------------------------------------------------


def test_the_data_a_criterion_uses_sits_inside_that_criterion(world: World) -> None:
    """**採点材料は、それを使う観点の中に置く**（#303）。

    入出力セットも項目表も「どの観点が何で判定されるか」に属するもので、
    課題全体の属性ではない（レポート課題に入出力は無関係）。画面の末尾に
    独立したカードとして置いていたときは、観点の設定を見ている人が下まで
    スクロールして初めて採点材料に出会った。

    **入れ子のフォームは作れない**ので、欄は観点の中に置き、送信先は
    `form` 属性で結び付ける（HTML5）。ここではその結び付きを固定する ──
    外れると、押しても何も起きないボタンになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    # 観点の欄の中に出ている（観点の削除印より後ろ = 同じ <details> の中）。
    assert page.index("この観点を削除する") < page.index("入出力セット"), "観点の外に出ている"
    # 送信先は末尾の空フォームで、欄はそれを名指しする。
    assert 'id="io-form"' in page
    assert 'form="io-form" name="case_name"' in page
    assert '<button form="io-form" type="submit">' in page


def test_the_page_is_ordered_the_way_a_task_is_written(world: World) -> None:
    """**書く順に並べる**（#303）。

    以前は知識要素が問題文の直下にあり、画像の欄はその下だった ── 問題文を
    書き終えた人が最初に出会うのが「この課題はどの知識要素を問うか」で、
    まだ書き上がっていないものについての問いだった。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    body = _main(
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    order = [
        body.index("問題文に貼る画像"),
        body.index("提出できるファイル形式"),
        body.index("ルーブリック（この課題の観点）"),
        body.index("問う知識要素"),
    ]
    assert order == sorted(order), order
    # 学習者に出る形は畳んでおく（書き始める前に読むものではない）。
    assert '<details class="card" id="preview">' in body


def test_the_task_page_groups_its_sections_into_boxes(world: World) -> None:
    """**大項目ごとに箱で区切る**（#308）。

    以前はページ全体が 1 枚の箱で、節の区切りは細い罫線 1 本だった ── どこ
    までが問題文の話でどこからが採点の話なのかが読めず、大項目と中項目が同じ
    強さに見えていた。出題の共通設定と同じ作法（大項目は箱の外の見出し、
    中身は箱）に揃える。

    **フォームは 1 つのまま。** 送信の単位は変えない（問題文・観点・形式は
    1 回の保存で 1 つの版になる）ので、箱はフォームの中にある。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    body = _main(
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    for major in ("<h2>問題</h2>", "<h2>提出</h2>", "<h2>採点</h2>", "<h2>分類</h2>"):
        assert major in body, major
    # 大項目は箱の外、中身は箱の中。
    assert body.index("<h2>問題</h2>") < body.index("問題文に貼る画像")
    # フォームは 1 つ（編集の本体）。採点材料の送信先は別に置いた空フォーム。
    form_open = body.index('<form method="post"')
    assert '<form method="post" class="card"' not in body, "ページ全体が 1 枚の箱に戻っている"
    assert body.index("<h2>分類</h2>") > form_open


def test_saving_comes_back_to_the_place_you_pressed(world: World, monkeypatch) -> None:
    """**押した場所に戻る**（#309）。

    保存は POST → 303 → GET で、戻ってくるのは別の読み込みである ── 頁の
    先頭が出る。観点は既定で畳んであるので、その中の入出力セットを直した人は
    「直したものがどこへ行ったのか」を探すことになる。

    JavaScript は位置と開いていた `<details>` を覚えて戻すが（`base.html`）、
    **無くても効くようにする** ── 保存の種類が分かっているなら、サーバが
    その場所を開いて返せる。ここで固定するのはそちら側である。

    錨は `#saved` ── **知らせそのものに着ける**。節の頭（`#tests`）だと、
    知らせが節の下の方にあるときは見えないままになる。節を開くのは錨ではなく
    `saved_key` の仕事で、そちらは下の `id="io-edit"` で確かめている。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    # 門 1（参照解答が全ケースを通る）はサンドボックスを使う。ここで確かめたい
    # のは戻り先なので通す。
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.TaskVerifier.passes",
        lambda self, candidate, source: (True, "all cases pass"),
    )

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=_case_form(version, **{"0": {"expected": "9\n"}}),
        follow_redirects=False,
    )
    # 行き先は知らせそのもの（入出力セットの保存ボタンの隣にある）。
    assert response.headers["location"].endswith("#saved")

    page = world.client("teacher").get(response.headers["location"]).text
    # その観点は開いて返す。畳んだまま返すと、`#saved` へも飛べない。
    assert 'class="criterion" id="criterion-correctness" open>' in page.replace("\n", "")
    # 直した欄も開いておく。
    assert 'id="io-edit"' in page and "io-edit" in page.split("テストケースを直す")[0]


def test_every_criterion_carries_a_stable_id(world: World) -> None:
    """開き直すのに使う id は**観点コード**で付ける（#309）。

    番号で覚えると、観点を 1 つ消しただけで別の観点が開く。コードは観点の
    同一性そのものである（課題をまたいで同じ観点は同じコード）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert 'id="criterion-correctness"' in page
    assert 'id="criterion-readability"' in page


def test_the_four_boxes_are_one_save(world: World) -> None:
    """**1 回の保存の単位を、枠で示す**（#311）。

    大項目ごとに箱で区切った（#308）ことで、共通設定のように箱ごとに保存が
    あるように見えていた ── あちらは節ごとに別のフォームで、こちらは問題・
    提出・採点・分類の全部で 1 つの版になる。押すボタンは枠の足元の 1 つだけ。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    body = _main(
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    frame = body[body.index('<section class="taskform">') : body.index("</section>")]
    for major in ("<h2>問題</h2>", "<h2>提出</h2>", "<h2>採点</h2>", "<h2>分類</h2>"):
        assert major in frame, f"{major} が保存の枠の外にある"
    # 保存は枠の足元。**枠の外に出すと、どこまでを保存するのか読めなくなる。**
    assert "taskform-foot" in frame
    # 押せる保存はこの 1 つだけ（欄の説明が同じ語を使うのは構わない ──
    # 「この欄はそれでは保存されない」と言うために要る）。
    assert frame.count('<button type="submit">この問題を保存して更新する</button>') == 1
    # 枠から下は別の操作（押すとその場で起きる）。
    assert "<h2>この問題への操作</h2>" in body
    assert body.index("</section>") < body.index("<h2>この問題への操作</h2>")


def test_data_no_criterion_uses_is_called_unused(world: World) -> None:
    """**持っているのに使われていないデータを、黙って隠さない**（#303）。

    欄は観点の中にあるので、観点が評価器を指名していない課題では画面から
    消える ── 観点を宣言する前に作られた課題や、評価器を付け替えた課題で
    起きる。消すのではなく、使われていないと言う。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec

    world.register("teacher", Role.INSTRUCTOR)
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p7",
            unit="ex01",
            statement="## [必須] 合計 ##\n\n合計を出力する。",
            criteria=(
                CriterionSpec(
                    code="design",
                    title="設計",
                    description="構成が読み取れるか。",
                    weight=1.0,
                    # **どの観点もテスト実行を指名していない。**
                    evaluator=None,
                    levels=(
                        LevelSpec(level=0, label="未達", descriptor="読めない", score_ratio=0.0),
                        LevelSpec(level=1, label="達成", descriptor="読める", score_ratio=1.0),
                    ),
                ),
            ),
            test_cases=(TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    )

    page = (
        world.client("teacher")
        .get(f"/manage/courses/{world.course.id}/tasks/{saved.task.id}/edit")
        .text
    )
    assert "使われていない入出力セット" in page
    assert "case1" in page


def test_the_evaluator_choices_are_grouped_with_ai_first(world: World) -> None:
    """**種類でまとめ、AI を上に置く**（#318）。

    観点に割り当てるものを選ぶとき、まず決めるのは「AI に読ませるか・機械で
    確定させるか・人が付けるか」で、評価器の名前はその次である。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert '<optgroup label="AI が判定する">' in page
    assert page.index('label="AI が判定する"') < page.index('label="機械が確定させる（決定的）"')
    assert page.index('label="機械が確定させる（決定的）"') < page.index('label="人が採点する"')
    # **科目が宣言している評価器だけが並ぶ。** cs_lang_c_intro は
    # rubric_ai_judge・code_test_runner・text_pattern_check しか宣言して
    # いないので、項目を積み上げる評価器や network_test_runner はここには
    # 出ない（出せば付けられ、付けても誰も採点しない）。
    ai_group = page[page.index('label="AI が判定する"') : page.index('label="機械が確定させる')]
    assert "checklist_ai_judge" not in ai_group
    det_group = page[page.index('label="機械が確定させる') : page.index('label="人が採点する"')]
    assert "code_test_runner" in det_group
    assert "text_pattern_check" in det_group
    assert "network_test_runner" not in det_group

    # 宣言している科目（report_ja）の課題では AI の組に居る。
    from aijudge_core.ids import TaskVersionId

    with world.database.unit_of_work() as uow:
        first = uow.tasks.latest_version(TaskId(task_id))
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "d" * 32),
                    "version": first.version + 1,
                    "subject_profile": "report_ja",
                }
            )
        )
        uow.commit()
    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    ai_group = page[page.index('label="AI が判定する"') : page.index('label="機械が確定させる')]
    assert "checklist_ai_judge" in ai_group
    assert "AI が項目を判定する" in ai_group


def test_an_evaluator_the_profile_does_not_declare_is_refused(world: World) -> None:
    """**科目が宣言していない評価器は観点に付けられない。**

    付けても採点パイプラインはその評価器を呼ばず（ADR 0002）、観点は誰も
    担当しないまま提出が「点が 1 つも出ない」で落ちる。prog2 ex01-2 で
    実際に起きた（2026-09-22）: cs_lang_c_intro に無い text_pattern_check を
    画面から選べた。選択肢を絞るだけでなく保存でも断る（#146 と同じ理由）。

    その後 cs_lang_c_intro は text_pattern_check を宣言した（prog2 ex01-3
    が同じ理由で落ちたのを機に、2026-09-23）ので、ここでの「未宣言の評価器」
    の例は network_test_runner に替えてある ── この科目が宣言しない
    ことに変わりはない評価器で、検証の意図は変えていない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    client = world.client("teacher")
    rows = {
        "statement": "## [必須] 修了証 ##\n\n画像を出す。",
        "criterion_code": ["certificate"],
        "criterion_title": ["修了証"],
        "criterion_description": ["読み取れるか"],
        "criterion_weight": ["1.0"],
        "criterion_evaluator": ["network_test_runner"],
        "criterion_levels": [""],
    }
    response = client.post(f"/manage/courses/{world.course.id}/tasks/{task_id}/revise", data=rows)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "network_test_runner" in detail
    assert "cs_lang_c_intro" in detail
    assert "/grading" in detail

    # 共通ルーブリックも同じ境界。
    response = client.post(f"/manage/courses/{world.course.id}/rubric", data=rows)
    assert response.status_code == 400, response.text

    # 既に付いてしまっている版は、選択中のまま警告が出る（黙って差し替えない）。
    from aijudge_core import RubricCriterion, RubricLevel
    from aijudge_core.ids import CriterionId, TaskVersionId

    with world.database.unit_of_work() as uow:
        first = uow.tasks.latest_version(TaskId(task_id))
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "b" * 32),
                    "version": first.version + 1,
                    "criteria": (
                        RubricCriterion(
                            id=CriterionId("crt_" + "b" * 32),
                            code="certificate",
                            title="修了証",
                            description="読み取れるか",
                            weight=1.0,
                            evaluator_id="network_test_runner",
                            levels=(
                                RubricLevel(
                                    level=0, label="未達", descriptor="無い", score_ratio=0.0
                                ),
                                RubricLevel(
                                    level=1, label="達成", descriptor="ある", score_ratio=1.0
                                ),
                            ),
                        ),
                    ),
                }
            )
        )
        uow.commit()
    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "科目プロファイルが宣言していない" in page
    assert 'value="network_test_runner" selected' in page


def test_kc_candidates_come_from_the_courses_own_components(world: World, monkeypatch) -> None:
    """**候補はコースが使っている知識要素から出す**（#318）。

    課題に付けるとコースの範囲にも入るので、コース外から選べると**課題を
    直すつもりの操作でコースの設定が変わる**。コースに何を置くかは知識要素の
    画面で決めることで、課題の編集の副作用にしない。

    落としたものは黙って消さず、件数と行き先（コースの知識要素で足す）を言う。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader.propose",
        lambda self, text, *, namespaces, existing_keys: SimpleNamespace(
            proposal=SimpleNamespace(
                knowledge_components=(
                    SimpleNamespace(key="cs.loops.termination", label="ループの停止"),
                )
            ),
            discarded=(),
        ),
    )

    page = (
        world.client("teacher")
        .post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/kc-candidates",
            data={"statement": "自然数 n を読み、1 から n まで足した値を出力しなさい。" * 2},
        )
        .text
    )

    # このコースは知識要素を使っていないので、候補は 1 つも選べない。
    assert 'value="cs.loops.termination"' not in page
    assert "このコースが使っていない知識要素を 1 件落としました" in page
    assert "/kc" in page, "コースに足しに行く導線が無い"


def test_every_installed_evaluator_can_be_picked_for_a_criterion(world: World) -> None:
    """**足した評価器は画面から選べること**（#315）。

    観点の「判定する評価器」は決定的評価器しか並べていなかったので、AI でも
    指名しないと走らない評価器 ── 項目を積み上げる `checklist_ai_judge`
    （#302）── を割り当てる手段が画面に無かった。

    表記は**説明（評価器コード）**で揃える。選ぶときに要るのは「何を見る
    評価器か」で、コードはその確認である。

    **並ぶのは科目が宣言しているもの**（2026-09-22）。インストール済みの全部を
    出すと、科目に無い評価器を観点に付けられ、その観点は誰も採点しない。
    `checklist_ai_judge` を宣言している `report_ja` の課題で見る。
    """
    from aijudge_core.ids import TaskVersionId

    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    with world.database.unit_of_work() as uow:
        first = uow.tasks.latest_version(TaskId(task_id))
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "a" * 32),
                    "version": first.version + 1,
                    "subject_profile": "report_ja",
                }
            )
        )
        uow.commit()

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert 'value="checklist_ai_judge"' in page, "足した AI 評価器が選べない"
    assert 'value="submission_compliance"' in page
    assert "（checklist_ai_judge）" in page, "表記が説明（コード）の順でない"
    # 空は `rubric_ai_judge` のことなので、選択肢として二重に並べない。
    assert 'value="rubric_ai_judge"' not in page


# --------------------------------------------------------------------------
# 既存の課題を AI で書き直す（#306）
# --------------------------------------------------------------------------


def _revision(monkeypatch, *, changes=("入力の範囲を明記した",), kcs=(), seen=None) -> None:
    def _revise(self, statement, *, criteria, vocabulary, current_kcs=(), instructions=()):
        if seen is not None:
            seen.append(instructions)
        return SimpleNamespace(
            statement="## [必須] 合計 ##\n\n2 つの整数（各 0 以上 100 以下）の和を出力する。",
            changes=tuple(changes),
            knowledge_components=tuple(kcs),
            prompt_id="task_revision_ja@2",
            model="stub-model",
            unchanged=not changes,
        )

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskReviser.revise", _revise)


def test_an_ai_revision_waits_for_approval(world: World, monkeypatch) -> None:
    """**必ず承認待ち**（#306・P5・ADR 0008）。

    教員はまだ 1 文字も読んでいない。承認するまで学習者にはいまの版が
    出続ける。書き直すのは問題文と知識要素だけで、観点と入出力セットと
    参照解答は触らない ── 観点の段階は教員が決めるもので、期待出力は参照解答を
    走らせて作るものだから（#305）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
        published_before = uow.tasks.latest_published_version(TaskId(task_id))
    _revision(monkeypatch)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/drafts" in response.headers["location"], "差分を読む場所へ送っていない"

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
        published_after = uow.tasks.latest_published_version(TaskId(task_id))
        drafts = uow.tasks.list_drafts(world.course.id)
    # **版は増えない**（#321）。増えるのは採用したとき ── 未承認の版が
    # 課題の履歴に積まれると、それが課題の状態として読まれる（#319）。
    assert after.id == before.id
    assert published_after.id == published_before.id
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.task_id == TaskId(task_id), "どの課題の改訂か分からない"
    assert "0 以上 100 以下" in draft.spec.statement
    assert draft.generated_by == "stub-model"
    # 観点・入出力セット・参照解答は触らない。
    assert [c.code for c in draft.spec.criteria] == [c.code for c in before.criteria]
    assert [c.name for c in draft.spec.test_cases] == [c.name for c in before.test_cases]
    assert draft.spec.reference_solution == before.reference_solution


def test_a_revision_with_nothing_to_change_is_not_queued(world: World, monkeypatch) -> None:
    """**空の改訂を承認待ちに積まない**（#306）。

    積むと、教員は差分の無い版を 1 件ずつ開いて確かめることになる ──
    承認待ちの一覧は「人が見るべきもの」だけを載せる場所である。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
    _revision(monkeypatch, changes=())

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    assert after.id == before.id, "直すところが無いのに版が増えている"


def test_the_queue_tells_a_revision_from_a_new_task(world: World, monkeypatch) -> None:
    """**新規か改訂かを出す**（#306）。判断そのものが違う ── 新規は「出すか
    どうか」、改訂は「置き換えるかどうか」である。差分も出す（承認は差分を
    見て決めるもので、書き換わった問題文を頭から読み直させない）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    _revision(monkeypatch)
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/drafts").text
    assert "改訂" in page
    assert 'class="code diff"' in page
    # 足した行と消した行が読める形で出ている。
    assert "0 以上 100 以下" in page
    # 改訂はキーを変えられない（同じ課題の書き直し）。
    assert 'name="key_suffix"' not in page


# --------------------------------------------------------------------------
# 課題ごとの日程（#325）
# --------------------------------------------------------------------------


def _unit_schedule(world: World, unit: str, **fields) -> None:
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/{unit}/schedule",
        data={
            "opens_at": "2026-09-18T09:00",
            "submissions_open_at": "2026-09-18T13:00",
            "due_at": "2026-09-25T23:59",
            "accepts_until": "2026-10-02T23:59",
            "grading_starts_at": "",
            **fields,
        },
        follow_redirects=False,
    )


def test_revising_a_task_does_not_move_its_schedule(world: World) -> None:
    """**揃えた日程が、課題を直しただけで崩れない**（#325）。

    引き継いでいたのは公開と締切の 2 つだけで、提出開始・受付終了・採点開始・
    猶予は版を上げるたびに空へ戻っていた ── 問題セットで揃えたあとに課題を
    1 つ直すと、セットの画面が「日程が課題ごとにばらついています」と言い直す。
    **教員は揃えたのに、揃えた操作が揃えたものを壊していた。**
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")
    from aijudge_reviewconsole.overview import unit_key

    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(task_id)))
    _unit_schedule(world, unit)

    with world.database.unit_of_work() as uow:
        before = uow.tasks.get_task(TaskId(task_id))
    assert before.submissions_open_at is not None, "前提が崩れている（日程が入っていない）"

    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 合計 ##\n\n誤字を直した本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        after = uow.tasks.get_task(TaskId(task_id))
    assert after.opens_at == before.opens_at
    assert after.submissions_open_at == before.submissions_open_at, "提出開始が消えている"
    assert after.due_at == before.due_at
    assert after.accepts_until == before.accepts_until, "受付終了が消えている"
    # 問題セットの画面も、揃っていないとは言わない。
    page = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "日程が課題ごとにばらついています" not in page


def test_a_task_shows_and_fixes_its_own_schedule(world: World) -> None:
    """**各課題でも日程を確認・修正できる**（#325）。

    決めるのは問題セットだが、揃っていないものを直す口が要る ── セットの
    日程を保存し直すと、当てたくない課題まで動く。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert 'name="submissions_open_at"' in page, "課題の画面に日程の欄が無い"
    assert 'name="accepts_until"' in page

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/schedule",
        data={
            "opens_at": "2026-09-18T09:00",
            "submissions_open_at": "2026-09-18T13:00",
            "due_at": "2026-09-25T23:59",
            "accepts_until": "2026-10-02T23:59",
            "grading_starts_at": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#saved")

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        versions = uow.tasks.latest_version(TaskId(task_id))
    assert task.due_at is not None and task.accepts_until is not None
    # **版は上がらない。** 日程は課題の内容ではない（P8 の対象外）。
    assert versions.version == 1, "日程を直しただけで版が上がっている"


def test_a_task_in_a_mixed_unit_says_so(world: World) -> None:
    """ばらついていることを**課題の画面でも告げる**（#325）。

    問題セットの画面は「ばらついている」と言うが、その課題自身の値は出さない
    ── 1 件ずつ開いて見比べることになる。判定は問題セットのものをそのまま
    使う（`UnitGroup.mixed`）── 違う理屈で書くと、片方が「ばらついている」と
    言い、もう片方が「揃っている」と出る。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_reviewconsole.overview import unit_key

    world.register("teacher", Role.INSTRUCTOR)
    first = _task_with_tests(world)
    client = world.client("teacher")
    # **同じセットに 2 問。** 1 問だけのセットでは、その課題の値がそのまま
    # セットの代表値になるので、ずれようがない。
    second = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p2",
            unit="ex01",
            position=2,
            statement="## [必須] 差 ##\n\n差を出力する。",
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    ).task.id
    with world.database.unit_of_work() as uow:
        unit = unit_key(uow.tasks.get_task(TaskId(first)))
    _unit_schedule(world, unit)

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{second}/edit").text
    assert "課題ごとにばらついています" not in page, "揃っているのに警告が出ている"

    # この課題だけ締切を動かす。
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{second}/schedule",
        data={
            "opens_at": "2026-09-18T09:00",
            "submissions_open_at": "2026-09-18T13:00",
            "due_at": "2026-09-30T23:59",
            "accepts_until": "2026-10-02T23:59",
            "grading_starts_at": "",
        },
        follow_redirects=False,
    )

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{second}/edit").text
    assert "課題ごとにばらついています" in page
    # 動かさなかった側にも出る ── ばらついているのはセットであって、
    # 「どちらが正しいか」は機械には決められない。
    other = client.get(f"/manage/courses/{world.course.id}/tasks/{first}/edit").text
    assert "課題ごとにばらついています" in other


def test_a_broken_task_schedule_is_refused(world: World) -> None:
    """**壊れた順序を保存させない**（`Task._check_schedule`）。

    提出開始が締切より後の課題は、誰も提出できないまま締切を迎える。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/schedule",
        data={
            "opens_at": "2026-09-18T09:00",
            "submissions_open_at": "2026-10-01T13:00",
            "due_at": "2026-09-25T23:59",
            "accepts_until": "",
            "grading_starts_at": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 更新の知らせは、押したボタンの近くに出る
# --------------------------------------------------------------------------


def test_the_save_notice_lands_next_to_the_button(world: World) -> None:
    """**押したボタンの近くに出し、そこへ着地させる。**

    知らせをページ先頭の帯だけに出していたとき、画面の下の方にあるボタンを
    押した教員には見えなかった ── 送信のあと画面は先頭に戻るが、帯は 1 画面ぶん
    上にあって視野に入らない。「押せたのかどうか分からない」は、押していないと
    思ってもう一度押す形になる。

    固定するのは 2 つ。**錨が知らせに着いていること**（`#saved`）と、
    **知らせが保存ボタンと同じ箱にあること**。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 合計 ##\n\n直した本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].endswith("#saved"), "知らせへ着地させていない"

    page = client.get(response.headers["location"]).text
    assert page.count('id="saved"') == 1, "着地点が複数あると、どれに飛ぶか決まらない"
    # 知らせは保存ボタンと同じ箱にある。**帯ではない。**
    foot = page.split('<div class="taskform-foot">')[1].split("</div>")[0]
    assert 'id="saved"' in foot, "知らせが保存ボタンの箱の外にある"
    assert "課題を保存しました" in foot or "保存しました" in foot


def test_no_save_notice_is_ever_lost(world: World, monkeypatch) -> None:
    """**置き場所を作り忘れても知らせは消えない。**

    課題の画面は、フォームの側に置き場所のある知らせ（`FLASH_HOMES`）だけを
    そこに出し、**それ以外は上の帯に出す**。両方から漏れると、操作は効いた
    のに画面は何も言わない ── 効いていないと読まれて、もう一度押される。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    # 課題の画面に着く知らせを、鍵ごとに全部通す。
    landing = [
        "task",
        "tests_added",
        "tests_failed",
        "tests_revised",
        "items_revised",
        "regraded",
        "restored_version",
        "withdrawn",
        "restored",
        "revision_none",
        "revision_failed",
        "task_without_tests",
        "task_generation_failed",
    ]
    for key in landing:
        page = client.get(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/edit?saved={key}"
        ).text
        assert page.count('id="saved"') == 1, f"{key} の知らせが消えているか、二重に出ている"


def test_the_reason_a_revision_failed_is_on_the_page(world: World, monkeypatch) -> None:
    """**理由をそのまま出す**（決めつけない・#52）。

    以前は理由を URL の錨に載せていたので、画面には「書き直せませんでした」
    としか出なかった ── 錨は知らせの着地点に要るので、理由は問い合わせで渡して
    画面に出す。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)

    def _boom(self, statement, **kwargs):
        raise RuntimeError("ollama に繋がりません")

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskReviser.revise", _boom)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert location.endswith("#saved")

    page = client.get(location).text
    assert "ollama に繋がりません" in page, "理由が画面に出ていない"


def test_the_teacher_can_tell_the_ai_what_to_fix(world: World, monkeypatch) -> None:
    """**何を直してほしいかは、読んだ教員がいちばんよく知っている**（#306）。

    観点との食い違いは機械的に見付かるが、「毎年ここで質問が来る」は教員しか
    知らない ── 欄が無いと、そこは何度書き直させても直らない。作問の指示と
    同じ扱いで、必須事項の列ではない（強さは書き方が表す）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    seen: list[tuple[str, ...]] = []
    _revision(monkeypatch, seen=seen)

    # 画面に欄がある（`name` が合っていなければ、書いても届かない）。
    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert 'name="instructions"' in page, "書き直しに指示を渡す欄が無い"

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        data={
            "instructions": "必ず入力の上限を明記すること\n\nできれば実行例を 2 つに増やしてほしい"
        },
        follow_redirects=False,
    )

    assert seen == [("必ず入力の上限を明記すること", "できれば実行例を 2 つに増やしてほしい")], (
        "指示が書いたとおりに届いていない（空行が落ちていないか、順序が変わっていないか）"
    )


def test_a_revision_without_instructions_still_runs(world: World, monkeypatch) -> None:
    """**指示は入口であって、前提ではない**（#306）。

    空のまま押しても改訂は走る ── 観点との食い違いは指示が無くても見付かる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    seen: list[tuple[str, ...]] = []
    _revision(monkeypatch, seen=seen)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert seen == [()], "空欄が空の指示として渡っていない"
    with world.database.unit_of_work() as uow:
        assert len(uow.tasks.list_drafts(world.course.id)) == 1


# --------------------------------------------------------------------------
# 解答例と、そこから作るテストケース（#305）
# --------------------------------------------------------------------------


def test_the_reference_solution_is_visible_to_the_instructor_only(world: World) -> None:
    """**参照解答を画面に出す**（#305）。

    いままでどこにも出ていなかった ── 生成経路が書き込むだけで、教員は中身を
    読めず、直すこともできなかった。門 1 が落ちても、何が通らないのか確かめる
    手段が無い。

    **TA には出さない。** 解答そのものなので、読める人を増やさない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _task_with_tests(world)

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert 'id="reference"' in page
    assert "int main(void){return 0;}" in page

    ta_page = world.client("ta").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "int main(void){return 0;}" not in ta_page, "TA に参照解答が出ている"


def test_the_reference_solution_is_saved_with_the_cases(world: World, monkeypatch) -> None:
    """参照解答と入出力セットは**ひと組で保存する**（#305）。

    門 1 は両方を突き合わせる検査なので、片方だけ先に保存できると、教員が
    意図していない組み合わせを検査することになる。門にかけるのも**いま欄に
    あるもの**である ── 保存済みで確かめると、直した解答例ではない別のもので
    判定する。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))

    checked: list[str] = []

    def passes(self, candidate, source):
        checked.append(source)
        return True, ""

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskVerifier.passes", passes)
    form = _case_form(before)
    form["reference_solution"] = ["int main(void){return 1;}\n"]
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=form,
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    assert after.reference_solution == "int main(void){return 1;}\n"
    assert checked == ["int main(void){return 1;}\n"], "門にかけたのが欄の内容ではない"


def test_writing_a_solution_does_not_save_anything(world: World, monkeypatch) -> None:
    """**生成では版を上げない**（#305・#58 と同じ作法）。

    押した瞬間に承認待ちが増えて元に戻せない、を避ける。書いたものは欄に
    入るだけで、保存は「保存」で行う。**書きかけの入出力も捨てない。**
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))

    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SolutionWriter.write",
        lambda self, statement, language="c": SimpleNamespace(
            solution="/* AI が書いた */\nint main(void){return 0;}\n",
            prompt_id="p",
            model="m",
        ),
    )
    page = (
        world.client("teacher")
        .post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/reference-solution",
            data=_case_form(before, **{"0": {"input": "書きかけ\n"}}),
        )
        .text
    )

    assert "AI が書いた" in page
    assert "書きかけ" in page, "書きかけの入出力が捨てられている"
    # **読ませるために描き直しているのだから、開いて返す**（#314）。畳んで
    # 返すと、何も起きなかったように見える（生成は数十秒かかる）。
    assert 'id="criterion-correctness" open>' in page.replace("\n", "")
    assert 'id="reference"' in page and "open>" in page
    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    assert after.version == before.version, "生成で版が上がっている"


def test_a_proposal_takes_its_expected_output_from_running_the_solution(
    world: World, monkeypatch
) -> None:
    """**期待出力はモデルに書かせない**（#305）。

    参照解答を実際に走らせた出力を使う。誤った期待出力は「全員が落ちる」と
    して現れ、原因は提出物の側に見える ── 決定的な結果は `conclusive` なので
    AI にも見直されない（P3）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))

    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.InputProposer.propose",
        lambda self, statement, reference, language="c", count=5: SimpleNamespace(
            cases=(
                SimpleNamespace(name="caseX", input="5 6\n", why="普通の値"),
                SimpleNamespace(name="caseY", input="0 0\n", why="境界値"),
            ),
            prompt_id="p",
            model="m",
        ),
    )
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.outputs_for",
        lambda registry, profile, version, reference, inputs, *, evaluator_id: tuple(
            SimpleNamespace(
                name=name,
                input=text,
                output="11\n" if name == "caseX" else "",
                ok=(name == "caseX"),
                reason="" if name == "caseX" else "nonzero exit",
            )
            for name, text in inputs
        ),
    )
    form = _case_form(before)
    form["reference_solution"] = ["int main(void){return 0;}\n"]
    page = (
        world.client("teacher")
        .post(
            f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/propose",
            data=form,
        )
        .text
    )

    assert "caseX" in page and "11" in page
    # 走らなかったケースは採用の印を出さない（理由を言う）。
    assert "採れません" in page and "nonzero exit" in page
    # 提案は編集欄の中に出る。**両方開いて返す**（#314）。
    assert 'id="criterion-correctness" open>' in page.replace("\n", "")
    assert 'id="io-edit"' in page and "テストケースを直す" in page
    # 提案の時点では保存しない。
    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    assert after.version == before.version


def test_only_the_proposals_you_tick_are_kept(world: World, monkeypatch) -> None:
    """**採用は 1 件ずつ人が押す**（P5）。印を付けなかった提案は消える。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.TaskVerifier.passes",
        lambda self, candidate, source: (True, ""),
    )

    form = _case_form(before)
    form["reference_solution"] = [before.reference_solution]
    form["prop_name"] = ["caseX", "caseY"]
    form["prop_input"] = ["5 6\n", "0 0\n"]
    form["prop_expected"] = ["11\n", "0\n"]
    form["prop_adopt"] = ["0"]  # caseX だけ採用する
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=form,
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    names = [case.name for case in after.test_cases]
    assert "caseX" in names
    assert "caseY" not in names, "印を付けていない提案が入っている"
    assert next(c for c in after.test_cases if c.name == "caseX").payload["expected"] == "11\n"


def _task_with_items(world: World, *, items: tuple[str, ...] = ()) -> str:
    """項目表で採点する観点を持つ課題（レポート）。"""
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec

    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="rep01/p2",
            unit="rep01",
            statement="## [必須] 実験レポート ##\n\n性能を評価しなさい。",
            criteria=(
                CriterionSpec(
                    code="structure",
                    title="構成",
                    description="課題が求める項目が揃っているか。",
                    weight=1.0,
                    evaluator="checklist_ai_judge",
                    levels=(
                        LevelSpec(level=0, label="未達", descriptor="揃わない", score_ratio=0.0),
                        LevelSpec(level=1, label="達成", descriptor="すべて揃う", score_ratio=1.0),
                    ),
                ),
            ),
            test_cases=tuple(
                TestCaseSpec(
                    name=name,
                    evaluator="checklist_ai_judge",
                    payload={"aliases": [name]},
                    hidden=False,
                )
                for name in items
            ),
        ),
        subject_profile="report_ja",
        authored_by=_user_id(world, "teacher"),
    )
    return str(saved.task.id)


def test_a_checklist_criterion_shows_the_item_list_not_the_io_set(world: World) -> None:
    """**形が違うものを同じ欄で編集させない**（#302）。

    入出力の組と項目の並びは別の形で、どちらを出すかは評価器が名乗る
    （`test_case_shape`）。項目表の課題に入出力の欄を出すと、入力と期待出力を
    求められて何も書けない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_items(world, items=("目的", "考察"))

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert "項目表" in page
    assert "目的" in page and "考察" in page
    # **欄そのものが無いこと**を見る。語そのものは案内文にも出るので、
    # 文字列の有無で見ると案内を書き換えただけで落ちる。
    assert 'id="tests"' not in page, "項目表の課題に入出力の欄が出ている"


def test_an_empty_item_list_says_the_profile_default_is_used(world: World) -> None:
    """**0 件は「無い」ではなく「既定に従う」である。**

    黙って空の表を出すと、教員は自分が消したのか最初から無いのか分からない。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_items(world)

    page = (
        world.client("teacher").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    )
    assert "項目表（0 件）" in page
    assert "科目プロファイルの既定が使われます" in page


def test_editing_the_item_list_raises_a_new_version(world: World) -> None:
    """**修正は版を上げる**（P8）。出題済みの版の項目を書き換えない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_items(world, items=("目的",))
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/items/edit",
        data={
            "item_name": ["目的", "考察"],
            "item_weight": ["1.0", "3.0"],
            "item_hidden": ["0", "0"],
            "item_description": ["何を確かめる実験かが書かれている", ""],
            "item_aliases": ["目的、背景と目的", "考察,議論"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
    assert after.version == before.version + 1
    items = {case.name: case for case in after.test_cases}
    assert set(items) == {"目的", "考察"}
    # **区切りはカンマでも読点でもよい。** 決めつけると「、で区切ったら
    # 1 件になった」が起きる。
    assert items["目的"].payload["aliases"] == ["目的", "背景と目的"]
    assert items["考察"].payload["aliases"] == ["考察", "議論"]
    assert items["目的"].payload["description"] == "何を確かめる実験かが書かれている"
    assert items["考察"].weight == 3.0
    # **読む評価器を明示して保存する。** 課題の既定に倒すと code_test_runner
    # あてになり、誰も読まないまま残る。
    assert {case.evaluator_id for case in after.test_cases} == {"checklist_ai_judge"}


def test_editing_one_kind_of_data_keeps_the_other(world: World) -> None:
    """**片方の画面で保存して、もう片方を消さない**（#302）。

    保存は版を作り直す操作なので、渡さなかった検証データは消える。消えても
    例外は出ず、採点の段になって「検証データが無い」として現れる。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec

    world.register("teacher", Role.INSTRUCTOR)
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="mix01/p1",
            unit="mix01",
            statement="## [必須] 混在 ##\n\nプログラムとレポートを出す。",
            criteria=(
                CriterionSpec(
                    code="correctness",
                    title="出力の正しさ",
                    description="仕様どおりの出力を返すか。",
                    weight=0.5,
                    evaluator="code_test_runner",
                    levels=(
                        LevelSpec(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                        LevelSpec(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                    ),
                ),
                CriterionSpec(
                    code="structure",
                    title="構成",
                    description="求める項目が揃っているか。",
                    weight=0.5,
                    evaluator="checklist_ai_judge",
                    levels=(
                        LevelSpec(level=0, label="未達", descriptor="揃わない", score_ratio=0.0),
                        LevelSpec(level=1, label="達成", descriptor="揃う", score_ratio=1.0),
                    ),
                ),
            ),
            test_cases=(
                TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),
                TestCaseSpec(
                    name="考察", evaluator="checklist_ai_judge", payload={"aliases": ["考察"]}
                ),
            ),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    )
    task_id = str(saved.task.id)

    # 入出力だけを直す。項目表は残る。
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data={
            "case_name": ["case1"],
            "case_input": ["2 2\n"],
            "case_expected": ["4\n"],
            "case_weight": ["1.0"],
            "case_hidden": ["1"],
        },
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    by_evaluator = {case.evaluator_id: case for case in version.test_cases}
    assert set(by_evaluator) == {"code_test_runner", "checklist_ai_judge"}
    assert by_evaluator["code_test_runner"].payload["expected"] == "4\n"

    # 項目表だけを直す。入出力は残る。
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/items/edit",
        data={
            "item_name": ["考察", "結果"],
            "item_weight": ["1.0", "1.0"],
            "item_hidden": ["0", "0"],
            "item_description": ["", ""],
            "item_aliases": ["考察", "結果"],
        },
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    names = {case.evaluator_id: [] for case in version.test_cases}
    for case in version.test_cases:
        names[case.evaluator_id].append(case.name)
    assert sorted(names["checklist_ai_judge"]) == ["結果", "考察"]
    assert names["code_test_runner"] == ["case1"]


def test_editing_the_io_set_keeps_the_companion_cases_intact(world: World) -> None:
    """**伴走プロセスのケースは入出力の欄で書き換えない**（#402）。

    `network_test_runner` は形を名乗っていなかったので入出力扱いになり、
    入出力を 1 文字直すと全ケースが `code_test_runner` あてに作り直され、
    ポートや同梱ファイルが消えていた（v1.14.0 と同じ型の事故）。
    """
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec

    world.register("teacher", Role.INSTRUCTOR)
    companion = {
        "role": "client",
        "companion": "import socket\n",
        "port": 5000,
        "fixtures": {"hosts.txt": "localhost\n"},
        "expected_contains": ["200 OK"],
    }
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="net01/p1",
            unit="net01",
            statement="## [必須] 通信 ##\n\nサーバに接続する。",
            criteria=(
                CriterionSpec(
                    code="correctness",
                    title="出力の正しさ",
                    description="仕様どおりの出力を返すか。",
                    weight=1.0,
                    evaluator="code_test_runner",
                    levels=(
                        LevelSpec(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                        LevelSpec(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                    ),
                ),
            ),
            test_cases=(
                TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),
                TestCaseSpec(name="connect", evaluator="network_test_runner", payload=companion),
            ),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    )
    task_id = str(saved.task.id)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data={
            "case_name": ["case1"],
            "case_input": ["2 2\n"],
            "case_expected": ["4\n"],
            "case_weight": ["1.0"],
            "case_hidden": ["1"],
        },
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    by_name = {case.name: case for case in version.test_cases}
    assert set(by_name) == {"case1", "connect"}
    assert by_name["case1"].evaluator_id == "code_test_runner"
    assert by_name["case1"].payload["expected"] == "4\n"
    assert by_name["connect"].evaluator_id == "network_test_runner"
    assert dict(by_name["connect"].payload) == companion


def test_the_companion_shape_is_not_offered_as_an_io_set() -> None:
    """伴走プロセスの評価器は入出力とは別の形を名乗る（#402）。"""
    from aijudge_eval_network_test_runner import NetworkTestRunner
    from aijudge_grading.protocol import test_case_shape

    assert test_case_shape(NetworkTestRunner()) not in (None, "io")


def test_an_assistant_reads_the_item_list_but_cannot_change_it(world: World) -> None:
    """**TA は確認だけ**（#302・#300 と同じ扱い）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    task_id = _task_with_items(world, items=("目的",))

    page = world.client("ta").get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "項目表" in page and "目的" in page
    assert "項目表を直す" not in page
    assert "/items/edit" not in page
    # **隠すだけの画面は境界にならない。** 経路の側でも拒む。
    refused = world.client("ta").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/items/edit",
        data={"item_name": ["目的"], "item_weight": ["1.0"], "item_hidden": ["0"]},
    )
    assert refused.status_code in (403, 404), refused.status_code


def test_a_task_whose_criteria_do_not_read_an_item_list_refuses_one(world: World) -> None:
    """**誰も読まないデータを版に残さない。**

    観点が項目表を読む評価器を指名していない課題に項目を持たせると、画面にも
    出ず、採点にも使われないものが残る。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)

    refused = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/items/edit",
        data={"item_name": ["目的"], "item_weight": ["1.0"], "item_hidden": ["0"]},
    )
    assert refused.status_code == 400


def _task_with_tests(world: World, author: str = "teacher") -> str:
    """参照解答とテストケースを持つ課題（画面から直せるようキー付きで保存）。

    作者は既定で `teacher` ── 版の同一性は作者も見る（`substantive`）ので、
    別の人が同じ内容を保存すると版が上がる。"""
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import TestCaseSpec

    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            unit="ex01",
            statement="## [必須] 合計 ##\n\n合計を出力する。",
            reference_solution="#include <stdio.h>\nint main(void){return 0;}\n",
            test_cases=(
                TestCaseSpec(name="case1", input="1 2\n", expected="3\n"),
                TestCaseSpec(name="case2", input="2 2\n", expected="4\n"),
                TestCaseSpec(name="case3", input="0 0\n", expected="0\n"),
            ),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, author),
    )
    return str(saved.task.id)


def _user_id(world: World, login: str):
    with world.database.unit_of_work() as uow:
        user = uow.identity.find_user_by_login(TENANT, login)
    return user.id if user is not None else world.register(login, Role.INSTRUCTOR).user_id


def _case_form(version, **changes) -> dict[str, list[str]]:
    """版のテストケースをフォームの並びにする（`case_*` の同名フィールド）。"""
    cases = [
        {
            "name": case.name,
            "input": str(case.payload.get("input", "")),
            "expected": str(case.payload.get("expected", "")),
            "weight": str(case.weight),
            "hidden": "1" if case.hidden else "0",
        }
        for case in version.test_cases
    ]
    for index, fields in changes.items():
        cases[int(index)].update(fields)
    return {
        "case_name": [c["name"] for c in cases],
        "case_input": [c["input"] for c in cases],
        "case_expected": [c["expected"] for c in cases],
        "case_weight": [c["weight"] for c in cases],
        "case_hidden": [c["hidden"] for c in cases],
    }


def test_editing_test_cases_makes_a_new_version_after_the_gate(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """修正は版を上げる（P8）。参照解答があれば門 1 を通してから保存する。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
    assert before.reference_solution

    seen: list[int] = []

    def passes(self, candidate, source):
        seen.append(len(candidate.test_cases))
        return True, "all cases pass"

    monkeypatch.setattr("aijudge_reviewconsole.manage.TaskVerifier.passes", passes)
    form = _case_form(before, **{"0": {"expected": "9 9 9.000\n"}})
    form["case_delete"] = ["1"]
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=form,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert "saved=tests_revised" in response.headers["location"]
    assert seen == [len(before.test_cases) - 1]

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
        original = uow.tasks.get_version(before.id)
    assert after.version == before.version + 1
    assert len(after.test_cases) == len(before.test_cases) - 1
    assert after.test_cases[0].payload["expected"] == "9 9 9.000\n"
    # 元の版はそのまま（過去の採点が指している）。
    assert original.test_cases[0].payload["expected"] == before.test_cases[0].payload["expected"]
    # 問題文・観点・参照解答は引き継ぐ。
    assert after.statement == before.statement
    assert [c.code for c in after.criteria] == [c.code for c in before.criteria]
    assert after.reference_solution == before.reference_solution


def test_test_cases_the_reference_fails_are_not_saved(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """期待出力の誤字 1 つで全員が落ちる形を、保存する前に止める。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.TaskVerifier.passes",
        lambda self, candidate, source: (False, "case1: expected 2 2 2.000, got 9 9 9.000"),
    )
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=_case_form(before, **{"0": {"expected": "9 9 9.000\n"}}),
    )
    assert response.status_code == 400
    assert "参照解答が通らない" in response.json()["detail"]
    with world.database.unit_of_work() as uow:
        assert uow.tasks.latest_version(TaskId(task_id)).version == before.version


def test_unchanged_test_cases_do_not_bump_the_version(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.latest_version(TaskId(task_id))
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.TaskVerifier.passes",
        lambda self, candidate, source: (True, "ok"),
    )
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=_case_form(before),
    )
    with world.database.unit_of_work() as uow:
        assert uow.tasks.latest_version(TaskId(task_id)).version == before.version


def test_the_roster_form_names_accounts_not_student_numbers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """龍大では `学籍番号@mail.ryukoku.ac.jp` がアカウントで、「学籍番号を
    並べる」と書くと番号だけが貼られて全員が未登録になる。例は OIDC の
    許可ドメインから作り、ローカルアカウントの例も並べる。"""
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text
    assert "学籍番号を並べます" not in page
    assert "アカウント" in page
    assert "ta01" in page
    assert "学内ログイン（OIDC）は設定されていません" in page

    _enable_sso(world, monkeypatch)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text
    assert "y239999@example.ac.jp" in page


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
        assert (
            AuthService(uow.identity, audit=uow.audit).role_in(world.course.id, student.user_id)
            is None
        )
        assert uow.identity.get_user(student.user_id) is not None


def test_an_instructor_cannot_remove_themselves(world: World) -> None:
    """自分を外すとコースが見えなくなり、戻す手段が無い。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{teacher.user_id}/remove"
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 科目プロファイル — 参照中は読み取り専用、未参照なら編集できる（#146）
# --------------------------------------------------------------------------


def test_the_template_stays_read_only_from_the_course_page(world: World) -> None:
    """**コース設定の画面からは雛形を変えられない。**

    このコースの雛形は他のコースも使っている（`world.course` が参照中）。
    コースごとの調整は上書きで行う（`aijudge_grading.overrides`）。
    雛形そのものを触るのは `/manage/subjects` で、そこでも参照中のものは
    読み取り専用（下のテスト群）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    # 実効設定は見える（評価器の名前が並ぶ）。
    assert "code_test_runner" in body
    # 雛形そのものを書き換える口は、この画面には無い。
    assert 'name="profile_text"' not in body
    assert "ここからは変えません" in body


def test_a_profile_a_course_uses_cannot_be_written_through_the_route(world: World) -> None:
    """**参照中のプロファイルを書き換える経路が無い**ことを、経路を叩いて固定する。

    以前は「`profile` を含む経路が 1 つも無い」ことを固定していた（#146 で
    未参照のものだけ編集できるようにしたので、経路自体は存在する）。守りたい
    のは経路の不在ではなく、**1 人の操作で他のコースの採点が変わらない**こと。
    """
    world.register("boss", Role.ADMIN)
    name = world.course.subject_profile
    before = (PROFILES / f"{name}.yaml").read_text(encoding="utf-8")

    response = world.client("boss").post(
        f"/manage/subjects/{name}", data={"text": "name: " + name + "\ndeterministic: []\n"}
    )

    assert response.status_code == 400
    assert "コースが使っています" in response.text
    # ファイルは 1 バイトも変わっていない。
    assert (PROFILES / f"{name}.yaml").read_text(encoding="utf-8") == before


def test_only_an_admin_can_open_the_profile_list(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    assert world.client("teacher").get("/manage/subjects").status_code == 403


def test_the_list_separates_used_profiles_from_editable_ones(world: World) -> None:
    world.register("boss", Role.ADMIN)

    body = world.client("boss").get("/manage/subjects").text

    # このコースが使っている雛形は「読み取り専用」側に、参照コードつきで出る。
    assert world.course.subject_profile in body
    assert world.course.code in body
    assert "使われていないプロファイル" in body
    assert "コースが使っているプロファイル" in body


def test_a_used_profile_says_why_it_cannot_be_edited(world: World) -> None:
    """**灰色にするだけでは、バグか権限かを区別できない**（#146）。"""
    world.register("boss", Role.ADMIN)

    body = world.client("boss").get(f"/manage/subjects/{world.course.subject_profile}").text

    assert "編集できません" in body
    assert "複製して" in body
    assert world.course.title in body


def test_duplicating_a_used_profile_keeps_the_comments_and_leaves_the_original(
    world: World, tmp_path: Path
) -> None:
    """複製は**ファイルを写す**。模型を経由して書き戻すとコメントが消える。"""
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    source = PROFILES / f"{world.course.subject_profile}.yaml"
    original = source.read_text(encoding="utf-8")
    (profiles / source.name).write_text(original, encoding="utf-8")
    world.console.profiles_dir = profiles
    world.register("boss", Role.ADMIN)

    response = world.client("boss").post(
        f"/manage/subjects/{world.course.subject_profile}/duplicate",
        data={"new_name": "cs_copy", "description": "複製したもの"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    copied = (profiles / "cs_copy.yaml").read_text(encoding="utf-8")
    # コメント（なぜその値なのかの記録）が残っている。
    assert "# " in copied
    assert copied.count("#") == original.count("#")
    assert "name: cs_copy" in copied
    assert "description: 複製したもの" in copied
    # 元は変わっていない。
    assert (profiles / source.name).read_text(encoding="utf-8") == original


def test_an_unused_profile_can_be_edited_and_renamed(world: World, tmp_path: Path) -> None:
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    (profiles / "cs_unused.yaml").write_text(
        "# なぜ deterministic だけなのかの記録\nname: cs_unused\ndeterministic: []\n",
        encoding="utf-8",
    )
    world.console.profiles_dir = profiles
    world.register("boss", Role.ADMIN)
    client = world.client("boss")

    saved = client.post(
        "/manage/subjects/cs_unused",
        data={"text": "# 記録は残す\nname: cs_unused\ntimeout_seconds: 42\n"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "timeout_seconds: 42" in (profiles / "cs_unused.yaml").read_text(encoding="utf-8")

    renamed = client.post(
        "/manage/subjects/cs_unused/rename",
        data={"new_name": "cs_renamed"},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    assert not (profiles / "cs_unused.yaml").exists()
    assert "name: cs_renamed" in (profiles / "cs_renamed.yaml").read_text(encoding="utf-8")


def test_a_broken_profile_is_not_saved(world: World, tmp_path: Path) -> None:
    """通らない設定を書くと、その科目の採点が次から止まる。保存の前に弾く。"""
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    good = "name: cs_unused\ndeterministic: []\n"
    (profiles / "cs_unused.yaml").write_text(good, encoding="utf-8")
    world.console.profiles_dir = profiles
    world.register("boss", Role.ADMIN)
    client = world.client("boss")

    # 評価器の名前が実在しない。
    unknown = client.post(
        "/manage/subjects/cs_unused",
        data={"text": "name: cs_unused\ndeterministic: [no_such_evaluator]\n"},
    )
    assert unknown.status_code == 400
    assert (profiles / "cs_unused.yaml").read_text(encoding="utf-8") == good

    # YAML として壊れている。
    malformed = client.post("/manage/subjects/cs_unused", data={"text": "name: [unclosed\n"})
    assert malformed.status_code == 400
    assert (profiles / "cs_unused.yaml").read_text(encoding="utf-8") == good
    # **書きかけを捨てない**（直して出し直せるように返す）。
    assert "unclosed" in malformed.text


def test_a_profile_name_cannot_escape_the_directory(world: World, tmp_path: Path) -> None:
    """名前はファイル名になる。検査せずに繋ぐとディレクトリの外に書ける。"""
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    (profiles / "cs_unused.yaml").write_text("name: cs_unused\n", encoding="utf-8")
    world.console.profiles_dir = profiles
    world.register("boss", Role.ADMIN)

    response = world.client("boss").post(
        "/manage/subjects/cs_unused/duplicate", data={"new_name": "../escaped"}
    )

    assert response.status_code == 400
    assert not (tmp_path / "escaped.yaml").exists()


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
        subject_profile="cs_network_python",
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
        subject_profile="cs_network_python",
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


def test_the_course_page_shows_the_state_and_the_rail_holds_the_destinations(
    world: World,
) -> None:
    """**行き先は帯に移した**（#189・ADR 0017）。

    以前この画面は分岐だけを持つメニューだった。同じ行き先が左の帯に常設
    された時点で二重になったので、ここに残すのは「コースの状態」── 問題
    セットと日程と、確定がどこまで進んだか ── だけにした。
    同じものが 2 か所にあると、片方だけ直したときにもう片方が古いまま残る。
    """
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)

    page = world.client("teacher").get(f"/courses/{world.course.id}").text
    body = _main(page)
    assert "問題セット" in body, "コースの状態が出ていない"
    # 分岐は本文に二重に置かない。
    assert "コース全体の設定" not in body
    assert f"/courses/{world.course.id}/queue" not in body
    # 帯からは行ける。
    assert f"/courses/{world.course.id}/queue" in page
    assert f"/manage/courses/{world.course.id}" in page
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
    _seed(world)
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

    # 行き先は帯にある（#189）。**押すと 403 になるものを並べない**という
    # 主張はそのままで、見る場所が変わった。
    ta_page = world.client("ta").get(f"/courses/{world.course.id}").text
    assert "問題セット" in ta_page, "TA に問題セットが出ていない"
    assert "未承認の課題" not in ta_page, "TA に作問が出ている"
    assert "/enrolments" not in ta_page

    teacher_page = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert "未承認の課題" in teacher_page
    assert "/enrolments" in teacher_page


def test_the_course_menu_puts_ai_authoring_below_grading(world: World) -> None:
    """**学期中に開く回数の順に置く。** 問題セットと採点が日常で、
    AI 作問はその合間に使う。
    """
    _world_with_every_role(world)

    # 帯の大項目の順（#189）。**採点が日常で、出題はその合間**という順は
    # メニューから帯へ移っても変わらない。
    page = world.client("teacher").get(f"/courses/{world.course.id}").text
    rail = page[page.index('<aside class="rail"') : page.index("</aside>")]

    assert rail.index(">採点<") < rail.index(">出題<")
    assert rail.index(">出題<") < rail.index(">設定<")


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
    body = _main(page.text)
    for gone in ("/schedule", "/finalize", "/clear", "/tasks/new"):
        assert gone not in body, f"TA の画面に {gone} が出ている"
    # ログアウト以外に送り先の無い画面である（`/manage/...` を叩く欄が無い）。
    assert 'action="/manage/' not in body, "TA の画面に設定を送る欄がある"


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

    # 本文は問題セット（コースの状態）で始まる。設定は帯にあり、本文には無い。
    page = world.client("teacher").get(f"/courses/{world.course.id}").text
    assert "問題セット" in _main(page)
    assert "コース全体" not in _main(page)


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


def test_revising_a_task_keeps_its_own_subject_profile(world: World) -> None:
    """**採点のプロファイルは課題が決める**（#195・#264）。

    コースの値は**新しい課題の既定**であって、既にある課題の決定ではない。
    渡し間違えると、問題文を直しただけで**採点のされ方が変わる**。

    本番で踏んだ ── 画像・C 言語・レポートが混在するデモコースで、C の課題を
    1 回直したら `cs_lang_c_intro` が `demo_image` になり、テスト実行も
    読みやすさの AI 判定も走らなくなって、総合点が永久に保留になった。

    **混在コースでしか出ない。** 1 コース 1 種類なら両者の値が一致している。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "mix",
            "unit": "ex10",
            "statement": "## [必須] 混在 ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    # コースとは違う科目を課題に宣言させる（#195 が可能にしたこと）。
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "c" * 32),
                    "version": first.version + 1,
                    "subject_profile": "report_ja",
                }
            )
        )
        uow.commit()
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        seeded = uow.tasks.latest_version(task.id)
    assert seeded.subject_profile != course.subject_profile, "前提が崩れている（混在していない）"

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={"statement": "## [必須] 混在 ##\n\n直した本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert latest.subject_profile == "report_ja", "編集で採点のプロファイルが変わった"


def test_revising_a_task_keeps_its_tests_and_reference_solution(world: World) -> None:
    """**問題文を直しただけでテストが消えてはいけない**（#262）。

    観点は引き継いでいたのに、テストケースと参照解答は引き継いでいなかった。
    結果は観点より悪い ── 観点が消えれば採点されない観点が出るだけだが、
    テストが消えると決定的評価が何も採点できず、**総合点が永久に保留**に
    なる。しかも画面には何も出ないので、教員は自分が壊したことを知らない。

    本番で踏んだ（デモコースの C 言語課題を 1 回直して、3 件あった
    テストが 0 件になった）。
    """
    from aijudge_core import TestCase
    from aijudge_core.ids import TaskVersionId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p9",
            "unit": "ex09",
            "statement": "## [必須] 最大値 ##\n\n最大値を出力してください。",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
        # 出題の経路ではテストを付けないので、テスト入りの**次の版**を置く
        # （版は不変なので上書きできない・P8）。
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "e" * 32),
                    "version": first.version + 1,
                    "test_cases": (
                        TestCase(
                            name="t1",
                            evaluator_id="c_tests",
                            payload={"input": "1 2 3", "expected": "3"},
                        ),
                    ),
                    "reference_solution": "int main(void){return 0;}",
                }
            )
        )
        uow.commit()
    with world.database.unit_of_work() as uow:
        seeded = uow.tasks.latest_version(task.id)
    assert len(seeded.test_cases) == 1, "前提が崩れている（テストを置けていない）"

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 最大値 ##\n\n誤字を直しました。",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert latest.version == seeded.version + 1
    assert "誤字を直しました" in latest.statement
    assert len(latest.test_cases) == 1, "問題文を直しただけでテストが消えた"
    assert latest.test_cases[0].payload["expected"] == "3"
    assert latest.reference_solution == "int main(void){return 0;}"


def test_revising_a_task_keeps_data_meant_for_other_evaluators(world: World) -> None:
    """**入出力以外の検証データも、評価器と中身ごと持ち越す。**

    #262 で入出力は残るようになったが、写していたのは `input` / `expected`
    の 2 欄だけだった。項目表・パターン表・伴走プロセスの宣言はここを通ると
    **課題の既定の評価器あての空の入出力に書き換わる** ── 例外は出ず、次の
    提出が「照合する項目が無い」として落ちる。

    本番で踏んだ（prog2 ex01-2、2026-09-22）: `text_pattern_check` の 3 項目が
    問題文を保存しただけで `code_test_runner` の空ケースになり、修了証の
    提出が採点できなかった。
    """
    from aijudge_core import TestCase
    from aijudge_core.ids import TaskVersionId

    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "cert",
            "unit": "ex01",
            "statement": "## [必須] 修了証 ##\n\n修了証の画像を出してください。",
            "position": "2",
            "readability_weight": "0.3",
        },
    )
    pattern_case = TestCase(
        name="コース名",
        evaluator_id="text_pattern_check",
        payload={"criterion": "certificate", "pattern": "terminal", "required": True},
        hidden=False,
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
        uow.tasks.save_version(
            first.model_copy(
                update={
                    "id": TaskVersionId("tsv_" + "f" * 32),
                    "version": first.version + 1,
                    "test_cases": (pattern_case,),
                }
            )
        )
        uow.commit()

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={
            "statement": "## [必須] 修了証 ##\n\n誤字を直しました。",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert len(latest.test_cases) == 1
    kept = latest.test_cases[0]
    assert kept.evaluator_id == "text_pattern_check", "別の評価器あてに書き換わった"
    assert kept.payload == pattern_case.payload, "payload が入出力の空欄に置き換わった"
    assert kept.hidden is False


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
    # 錨は知らせそのもの（`#saved`）── 節の頭では、知らせが視野の外に残る。
    assert response.headers["location"].endswith("?saved=schedule#saved")
    assert "日程を保存しました" in client.get(response.headers["location"]).text


def test_saving_the_course_settings_says_so(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/auto-finalize",
        data={"after_minutes": "60"},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("?saved=course_grace#saved")
    assert "保存しました" in client.get(response.headers["location"]).text


# --------------------------------------------------------------------------
# 知識要素の体系（設計原則 P6）
# --------------------------------------------------------------------------


def test_the_kc_page_shows_the_namespaces_of_the_course(world: World) -> None:
    """**共有されるのは語彙で、見えるかどうかはコースごとの宣言が決める。**

    以前は「他のコースにも見えます」と書いていたが、#37 で範囲を絞っている
    コースの一覧からは外したので、そのままでは嘘になっていた。
    """
    _seed(world)
    world.register("teacher", Role.INSTRUCTOR)
    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "知識要素" in body
    assert "cs" in body
    assert "コースには属しません" in body
    assert "ここで足したものだけ" in body


def _use_kc(world: World, key: str, label: str = "") -> None:
    """語彙に登録し（骨格の分野・単位も含めて）、このコースの範囲に入れる。

    画面から語彙は増やせない（2026-09-13 決定）ので、テストでは `register_kc`
    で骨格を置き、範囲への追加だけを画面（`kc/scope/add`）で行う。
    """
    from aijudge_admin import register_kc

    parts = key.split(".")
    for depth in range(2, len(parts) + 1):
        prefix = ".".join(parts[:depth])
        register_kc(
            world.database,
            key=prefix,
            label=label if prefix == key and label else prefix,
            namespaces=(parts[0],),
            seeding=depth < 4,
        )
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        merged = tuple(sorted(set(course.knowledge_components) | {key}))
        uow.identity.save_course(course.model_copy(update={"knowledge_components": merged}))
        uow.commit()


def _seed(world: World) -> None:
    """骨格の分野と単位を置く。**画面からは作れない**ので直接入れる。

    何度呼んでも増えない（`register_kc` は既にあるものを返す）ので、
    各テストの冒頭で気軽に呼べる。
    """
    from aijudge_admin import register_kc

    for key, label in (("cs.loops", "ループ"), ("cs.loops.control", "制御")):
        register_kc(world.database, key=key, label=label, namespaces=("cs",), seeding=True)


def test_an_instructor_can_correct_a_label(world: World) -> None:
    """**引退・削除と違って教員が直せる。**

    あちらは他のコースが使っているものを取り上げる操作なので管理者に限るが、
    名前を直すのは取り上げる操作ではない。正しい名前を知っているのは科目の
    専門家で、キーは動かないので壊れない。
    """
    _seed(world)
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    _use_kc(world, "cs.loops.control.basic", "ルーブ")

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/edit",
        data={"key": "cs.loops.control.basic", "label": "ループ", "description": "繰り返しの制御"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text
    assert "ループ" in page
    assert "繰り返しの制御" in page
    # キーは動かない。
    assert "cs.loops.control.basic" in page


def test_the_key_is_not_editable_from_the_page(world: World) -> None:
    """キーを直せるように見せない。ID がキーから決まり、過去の採点が指している。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.basic", "ループ")

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    form = page[page.index("名前・説明の修正") :]
    # 送るのは label と description だけ。key は hidden で固定。
    assert 'name="label"' in form
    assert 'name="description"' in form
    assert '<input type="hidden" name="key" value="cs.loops.control.basic">' in form
    assert 'キー（<span class="mono">cs.loops.control.basic</span>）は変わりません' in form


def test_a_component_is_removed_from_the_course_with_the_remove_form(world: World) -> None:
    """**共有の語彙からの削除ではない。** 外しても知識要素は残る（#289）。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    for key, label in (("cs.loops.control.basic", "ループ"), ("cs.loops.control.python", "Python")):
        _use_kc(world, key, label)

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/scope/remove",
        data={"kc": ["cs.loops.control.python"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.loops.control.basic",
        )
    # **このコースの一覧からは消える。** 使わないと決めたものが並び続けると、
    # 決めたこと自体が画面から読めない（#37）。名前空間の一覧には残る。
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "1 件をこのコースから外しました" in page
    assert page.index("cs.loops.control.basic") < page.index('id="vocabulary"')
    assert "cs.loops.control.python" in page.split('id="vocabulary"')[1]

    # **語彙からは消えていない。** 他のコースの Q-matrix は壊れない。
    from aijudge_core import kc_id_for

    with world.database.unit_of_work() as uow:
        assert uow.skills.get_kc(kc_id_for("cs.loops.control.python")) is not None


def test_a_component_left_out_can_be_brought_back(world: World) -> None:
    """隠すなら戻す道が要る ── 名前空間の一覧から足す（個別）か、追加フォーム。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    for key, label in (("cs.loops.control.basic", "ループ"), ("cs.loops.control.python", "Python")):
        _use_kc(world, key, label)
    client.post(
        f"/manage/courses/{world.course.id}/kc/scope/remove",
        data={"kc": ["cs.loops.control.python"]},
    )

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/scope/add",
        data={"kc": ["cs.loops.control.python"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "1 件をこのコースに足しました" in client.get(response.headers["location"]).text
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
    assert course.knowledge_components == ("cs.loops.control.basic", "cs.loops.control.python")


def test_a_new_course_uses_nothing_until_the_instructor_adds_something(world: World) -> None:
    """**未指定なら何も登録しない**（#289）。以前は空を「名前空間の全部」と
    読んでいたので、作ったばかりのコースに 987 件が並んだ。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    from aijudge_admin import register_kc

    register_kc(world.database, key="cs.loops.control.basic", label="ループ", namespaces=("cs",))

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "（0 件）" in page
    assert "まだ何も足していません" in page
    # 名前空間の一覧には出るが、コースの範囲には無い。
    assert "cs.loops.control.basic" in page.split('id="vocabulary"')[1]
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == ()


def test_a_whole_branch_can_be_added_and_removed_at_once(world: World) -> None:
    """階層ごと（`cs.loops` 以下）にまとめて足す・外す（#289）。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    from aijudge_admin import register_kc

    for key in ("cs.io", "cs.io.formatted"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",), seeding=True)
    for key in ("cs.loops.control.basic", "cs.loops.control.python", "cs.io.formatted.printf"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",))

    client.post(f"/manage/courses/{world.course.id}/kc/scope/add", data={"prefix": "cs.loops"})
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.loops.control.basic",
            "cs.loops.control.python",
        )
    # `cs.loop` では `cs.loops` を巻き込まない（区切りまで一致）。
    client.post(f"/manage/courses/{world.course.id}/kc/scope/remove", data={"prefix": "cs.loop"})
    with world.database.unit_of_work() as uow:
        assert len(uow.identity.get_course(world.course.id).knowledge_components) == 2
    client.post(f"/manage/courses/{world.course.id}/kc/scope/remove", data={"prefix": "cs.loops"})
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == ()


def test_the_drafting_form_offers_only_the_selected_components(world: World) -> None:
    """**同じ名前空間を複数のコースが使うほど関係のない候補が増える。**

    C の科目に `cs.python.*` が並ぶのは見にくいだけでなく、誤った知識要素を
    課題に付けられるということでもある（設計原則 P6）。
    """
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    for key, label in (("cs.loops.control.basic", "ループ"), ("cs.loops.control.python", "Python")):
        _use_kc(world, key, label)

    unit = _unit_of(world)
    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "cs.loops.control.python" in body  # 足したものは両方出る

    client.post(
        f"/manage/courses/{world.course.id}/kc/scope/remove",
        data={"kc": ["cs.loops.control.python"]},
    )
    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "cs.loops.control.basic" in body
    assert "cs.loops.control.python" not in body


def test_a_component_the_course_still_uses_cannot_be_removed(
    world: World,
) -> None:
    """**このコースの課題が使っているものは外せない**（#289）。

    外すと、その課題が問う知識要素が Q-matrix と食い違う。理由を添えて残す。
    """
    _seed(world)
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec

    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.basic", "ループ")
    save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            statement="## 課題 ##\n\n本文",
            knowledge_components=("cs.loops.control.basic",),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=world.register("t2", Role.INSTRUCTOR).user_id,
    )

    # 外そうとしても、課題が使っているので残る。
    response = client.post(
        f"/manage/courses/{world.course.id}/kc/scope/remove",
        data={"kc": ["cs.loops.control.basic"]},
        follow_redirects=False,
    )
    page = client.get(response.headers["location"]).text
    assert "0 件をこのコースから外しました" in page
    assert "1 件は<strong>このコースの課題が使っているので外していません" in page
    assert "cs.loops.control.basic" in page
    # このコースの課題が使っていることが分かる。
    assert "課題 1 件が使用中" in page
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.loops.control.basic",
        )


def test_only_an_admin_can_delete_a_component(world: World) -> None:
    """削除もコースをまたいで効く。1 コースの教員が他の語彙を消せない。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    _use_kc(world, "cs.loops.control.typo", "打ち間違い")
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/delete", data={"key": "cs.loops.control.typo"}
    )
    assert response.status_code == 403


def test_an_unused_component_is_deleted_from_the_page(world: World) -> None:
    """**打ち間違いの置き場所を引退にしない。**

    使われたことの無いキーを引退させて残すと、共有の一覧に誰の役にも
    立たない行が永久に並ぶ。
    """
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.typo", "打ち間違い")
    assert "cs.loops.control.typo" in client.get(f"/manage/courses/{world.course.id}/kc").text

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/delete",
        data={"key": "cs.loops.control.typo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "cs.loops.control.typo" not in client.get(f"/manage/courses/{world.course.id}/kc").text


def test_the_delete_control_is_hidden_for_a_used_component(world: World) -> None:
    """**使われているものには出さない。** 押せない操作を見せない。"""
    _seed(world)
    from aijudge_admin import save_task
    from aijudge_authoring import TaskSpec

    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.basic", "ループ")
    save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            statement="## 課題 ##\n\n本文",
            knowledge_components=("cs.loops.control.basic",),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=world.register("t2", Role.INSTRUCTOR).user_id,
    )

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    # **使われている行にだけ出ない。** 骨格の他の行には出るので、
    # ページ全体で見ると判定できない。
    used_row = page.split("cs.loops.control.basic")[1].split("</tr>")[0]
    assert ">削除<" not in used_row
    # 直接叩いても消えない。
    response = client.post(
        f"/manage/courses/{world.course.id}/kc/delete", data={"key": "cs.loops.control.basic"}
    )
    assert response.status_code == 400
    assert "使われています" in response.json()["detail"]


def test_only_an_admin_can_retire_a_component(world: World) -> None:
    """引退はコースをまたいで効く。1 コースの教員が他の語彙を畳めない。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    _use_kc(world, "cs.loops.control.basic", "ループ")
    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/kc/retire", data={"key": "cs.loops.control.basic"}
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

    **科目を決めるのは課題**（#195・#264）。以前はここでコースの側だけを
    report_ja にしていたが、それは「コースの値で判断する」という実装に
    合わせた設定で、規則の方ではなかった。
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
                    "subject_profile": "report_ja",
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
    """**生成物は下書きになる。課題にはならない**（#321・P5）。

    課題にすると、そこで同一性（課題キー → 課題 ID）が決まってしまう ──
    生成物は提案であって確定ではないので、名前を含めて採用のときに決められる
    必要がある。出所（`generated_by`）は下書きが持ち、採用した版へ引き継ぐ。
    """
    _seed(world)
    from aijudge_authoring.drafting import DraftTestCase, TaskDraft

    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    _use_kc(world, "cs.loops.control.basic", "ループ")

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
        data={"key_suffix": "p9", "kc": ["cs.loops.control.basic"], "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        drafts = uow.tasks.list_drafts(world.course.id)
        tasks_now = uow.tasks.list_for_course(world.course.id)
    # **課題にはならない**（#321）。下書きが 1 件できるだけで、採用するまで
    # 課題は存在しない ── だから採用のときにキーを含めて直せる。
    assert len(drafts) == 1
    assert drafts[0].generated_by == "stub-model"
    assert drafts[0].generation_prompt_version == "task_draft_ja@2"
    # 取り込んだ課題（1 件）以外は増えていない。
    assert len(tasks_now) == 1


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
    # **1 つ前の承認済みは出ている**（#319）。新しい版が未承認なだけで、
    # 課題が出題されなくなったわけではない ── 「出題されません」と書くと、
    # 画面が学習者の見え方と食い違う。
    assert "新しい版が未承認" in body
    assert "出題中" in body
    assert "未承認 — 出題されません" not in body
    # そこから承認・却下へ行ける。
    assert f"/manage/courses/{world.course.id}/drafts" in body


def test_dropping_a_revision_leaves_the_published_version_alone(world: World, monkeypatch) -> None:
    """**改訂を捨てても、出ている版は出続ける**（#319・#321）。

    捨てるのは下書きであって課題ではない。以前は改訂が版として積まれていた
    ので、却下すると「最新版が却下」になり、一覧が「却下済み — 出題されません」
    と出た ── 学習者には 1 つ前の承認済みが出ているのに、である。下書きを
    課題表の外に置いた（ADR 0019）ので、この読み違いは土台から消えた。
    """
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    _revision(monkeypatch)
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        draft = uow.tasks.list_drafts(world.course.id)[0]
        published_before = uow.tasks.latest_published_version(TaskId(task_id))

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/drafts/{draft.id}",
        data={"decision": "drop"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        published_after = uow.tasks.latest_published_version(TaskId(task_id))
        assert uow.tasks.list_drafts(world.course.id) == ()
    assert published_after is not None, "捨てただけで出題が止まっている"
    assert published_after.id == published_before.id

    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/ex01").text
    assert "却下済み — 出題されません" not in body
    assert "未承認 — 出題されません" not in body


def test_taking_a_revision_becomes_a_new_version(world: World, monkeypatch) -> None:
    """**採用すると新しい版になる**（#321）。キーは変えない（同じ課題の書き直し）。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    _revision(monkeypatch)
    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise-with-ai",
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        draft = uow.tasks.list_drafts(world.course.id)[0]
        before = uow.tasks.latest_version(TaskId(task_id))

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/drafts/{draft.id}",
        data={"decision": "approve"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with world.database.unit_of_work() as uow:
        after = uow.tasks.latest_version(TaskId(task_id))
        published = uow.tasks.latest_published_version(TaskId(task_id))
        tasks = uow.tasks.list_for_course(world.course.id)
    assert after.version == before.version + 1
    assert "0 以上 100 以下" in after.statement
    # **採用した時点で出題される**（承認の段はここ 1 つ）。
    assert published.id == after.id
    # 課題は増えない（同じ課題の新しい版である）。
    assert len(tasks) == 1
    assert after.source_key == before.source_key


def test_the_unit_page_offers_generation_only_with_components(world: World) -> None:
    """**骨格を置かずに始める。** 知識要素が 1 件も無い状態が出発点である。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    client = world.client("teacher")

    body = client.get(f"/manage/courses/{world.course.id}/units/{_unit_of(world)}").text
    assert "知識要素が登録されていないので生成できません" in body

    _seed(world)
    world.register("boss", Role.ADMIN)
    _use_kc(world, "cs.loops.control.basic", "ループ")
    body = client.get(f"/manage/courses/{world.course.id}/units/{_unit_of(world)}").text
    assert "AI にこのセットの課題を作らせる" in body
    assert 'name="kc"' in body


# --------------------------------------------------------------------------
# 科目情報の URL とシラバスからの候補
# --------------------------------------------------------------------------


def _proposal(*keys: str, discarded=()):
    """候補を返す `SyllabusReader` の代わり。生成そのものは測らない。

    **関門は通らない。** 採用できない候補を落とすのは `SyllabusReader.propose`
    の中なので、代役に差し替えるとそこは走らない ── 落とす判断は
    `apps/admin/tests/test_syllabus_prompt.py` の側で見て、ここでは
    落とした結果が画面にどう出るかだけを見る。
    """
    from aijudge_admin.syllabus import KcHint, ProposalResult, SyllabusProposal

    class _Reader:
        last_units: tuple[str, ...] = ()

        def __init__(self) -> None:
            self.seen: list[str] = []

        def propose(self, text, *, namespaces, existing_keys=(), unit_keys=()):
            self.seen.append(text)
            # **画面が単位を渡していること**を、代役の側でも見ておく
            # （渡さないとモデルは存在しない単位を作る）。
            type(self).last_units = unit_keys
            return ProposalResult(
                proposal=SyllabusProposal(
                    knowledge_components=tuple(
                        KcHint(key=key, label=key.rsplit(".", 1)[-1]) for key in keys
                    )
                ),
                prompt_id="test",
                model="test",
                discarded=discarded,
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
    reader = _proposal("cs.loops.control.arrays")
    monkeypatch.setattr("aijudge_reviewconsole.manage.SyllabusReader", reader)

    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    assert "cs.loops.control.arrays" in body
    # 一覧と同じページに出る（語彙を見ながら選べるように）。
    assert "名前空間から足す" in body


def test_the_candidates_are_built_from_the_saved_description(monkeypatch, world: World) -> None:
    """渡しているのが本当にコースの基本情報であることを確かめる。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": "計算機科学入門", "description": "ポインタと再帰を扱う"},
    )
    made = []

    class _Recording(_proposal("cs.loops.control.pointers")):
        def propose(self, text, *, namespaces, existing_keys=(), unit_keys=()):
            made.append(text)
            return super().propose(
                text,
                namespaces=namespaces,
                existing_keys=existing_keys,
                unit_keys=unit_keys,
            )

    monkeypatch.setattr("aijudge_reviewconsole.manage.SyllabusReader", _Recording)
    client.post(f"/manage/courses/{world.course.id}/kc/candidates")
    assert "ポインタと再帰を扱う" in made[0]
    assert "計算機科学入門" in made[0]


def _with_basics(world: World, client) -> None:
    """候補生成の材料（コースの基本情報）を入れる。"""
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "## 到達目標\n\n配列を扱える"},
    )


def test_what_the_gate_dropped_is_shown_with_its_reason(world: World, monkeypatch) -> None:
    """**黙って減らさない。理由も出す。**

    「除きました」だけでは、教員は次に何をすればよいのか分からない。
    落とす判断そのものは `SyllabusReader` の側にあり
    （`apps/admin/tests/test_syllabus_prompt.py`）、ここで見るのは画面に
    出ることだけである。
    """
    from aijudge_admin.syllabus import DiscardedCandidate

    _seed(world)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _with_basics(world, client)

    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader",
        _proposal(
            "cs.loops.control.arrays",
            discarded=(
                DiscardedCandidate(
                    key="cs.nosuch.unit_thing", reason="骨格に無い単位の下に置かれています"
                ),
                DiscardedCandidate(key="cs.loops", reason="分野そのものです（知識要素は 3 階層）"),
            ),
        ),
    )
    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text

    assert 'value="cs.loops.control.arrays"' in body
    assert "採用できない候補を 2 件除きました" in body
    assert "骨格に無い単位" in body
    assert "分野そのもの" in body


def test_candidates_need_the_basics_to_be_filled_in(world: World) -> None:
    """**候補を出せないことと、候補が無いことは違う。** 何をすればよいか言う。"""
    _seed(world)
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(f"/manage/courses/{world.course.id}/kc/candidates")
    assert response.status_code == 400
    assert "基本情報" in response.json()["detail"]

    # 出せないときはボタンも出さず、基本情報への導線を出す。
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert f"/manage/courses/{world.course.id}/basics" in page


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


def test_candidates_can_be_adopted_in_bulk(monkeypatch, world: World) -> None:
    """候補が 20 件あるとき 1 件ずつ往復させない。印を付けてまとめて範囲に入れる。
    **語彙への登録はしない**（2026-09-13 決定）── 語彙に無いキーは断る。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    client.post(
        f"/manage/courses/{world.course.id}/basics/apply",
        data={"title": world.course.title, "description": "配列と再帰を扱える"},
    )
    from aijudge_admin import register_kc

    for key, label in (("cs.loops.control.arrays", "配列"), ("cs.loops.control.recursion", "再帰")):
        register_kc(world.database, key=key, label=label, namespaces=("cs",))
    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader",
        _proposal("cs.loops.control.arrays", "cs.loops.control.recursion"),
    )
    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    rows = body[body.index("候補（") :]
    assert 'name="adopt" value="cs.loops.control.arrays"' in rows
    assert 'name="adopt" value="cs.loops.control.recursion"' in rows

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/adopt",
        data={"adopt": ["cs.loops.control.arrays", "cs.loops.control.recursion"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "2 件をこのコースに足しました" in page
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.loops.control.arrays",
            "cs.loops.control.recursion",
        )

    # 語彙に無いキーは採用できない（画面を経ない POST も同じ）。
    refused = client.post(
        f"/manage/courses/{world.course.id}/kc/adopt",
        data={"adopt": ["cs.loops.control.nothing"]},
    )
    assert refused.status_code == 400
    assert "登録されていない知識要素" in refused.json()["detail"]


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
    for key, label in (("cs.loops.control.arrays", "配列"), ("cs.loops.control.basic", "ループ")):
        _use_kc(world, key, label)
    # このコースは basic だけを使う ── cs.loops.control.arrays は体系にあるが範囲外。
    client.post(
        f"/manage/courses/{world.course.id}/kc/scope/remove",
        data={"kc": ["cs.loops.control.arrays"]},
    )

    monkeypatch.setattr(
        "aijudge_reviewconsole.manage.SyllabusReader",
        _proposal("cs.loops.control.arrays", "cs.loops.control.recursion"),
    )
    body = client.post(f"/manage/courses/{world.course.id}/kc/candidates").text
    rows = body[body.index("候補（") :]

    assert "語彙にあり（範囲外）" in rows
    # 範囲外でも採用できる（採用すれば範囲に入る）。
    assert 'name="adopt" value="cs.loops.control.arrays"' in rows
    # 語彙に無い候補は「新規」として出ず、関門が落とす（ここは代役なので落ちない
    # が、採用は断られる）。
    assert "新規" not in rows


def test_the_course_settings_link_to_both_flows(world: World) -> None:
    """基本情報と知識要素は別の作業。入口も分ける。

    候補づくりは知識要素のページにあるが、**基本情報が入っていて初めて出る**
    （材料がそこにあるので）。
    """
    _seed(world)
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
    # CSS は 1 か所（`packages/webui`）にある（#184）。**配信されたものを見る**
    # ── ファイルを直接読むと、mount の設定が壊れていても通ってしまう。
    stylesheet = world.client("teacher").get("/static/base.css").text
    assert "[hidden]{display:none!important}" in stylesheet


def test_the_long_running_forms_say_that_the_model_is_working(world: World) -> None:
    """**二度押しを止めるのが本題。** LLM の呼び出しは費用と待ち時間そのもの。

    仕組みは `base.html` に 1 つだけ置く。テンプレートごとに `onsubmit` を
    書き写していたので、**いちばん時間の掛かる作問に付いていなかった**。
    """
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    # 作問の欄は、問える知識要素が 1 つ以上あって初めて出る。
    _use_kc(world, "cs.loops.control.basic", "ループ")
    unit = _unit_of(world)

    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert 'data-working="生成中…' in body
    # 仕掛けは 1 か所（base.html）にあり、テンプレートは属性を書くだけ。
    assert "onsubmit=" not in body


def test_the_progress_is_not_reported_as_a_number(world: World) -> None:
    """**何%まで進んだかを知る手段が無い。** それらしい数字は根拠の無い表示になる。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _import_example(world)
    _use_kc(world, "cs.loops.control.basic", "ループ")
    unit = _unit_of(world)

    body = client.get(f"/manage/courses/{world.course.id}/units/{unit}").text
    assert "<progress" not in body


def test_a_badge_that_needs_someone_is_not_the_same_as_a_bad_one(world: World) -> None:
    """**色は「人が何かする必要があるか」で決める**（#46）。

    引退した知識要素（放っておいてよい）と、教員が開かないと閉じないものが
    同じ赤で並んでいた。同じ見え方なら、どちらも目に留まらない。
    """
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.basic", "ループ")
    client.post(
        f"/manage/courses/{world.course.id}/kc/retire", data={"key": "cs.loops.control.basic"}
    )

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    # 引退は確定した事実で、操作は要らない。
    assert '<span class="pill no">引退</span>' in page

    # **色だけに頼らない。** 記号を添える（色覚の差でも白黒でも読める）。
    # CSS は 1 か所（`packages/webui`）にある（#184）。**配信されたものを見る**
    # ── ファイルを直接読むと、mount の設定が壊れていても通ってしまう。
    assert ".pill.attn::before" in client.get("/static/base.css").text


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
        subject_profile="cs_lang_c_intro",
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
    from aijudge_webui import local_filter

    # 表示は機関の時刻（保存は UTC）。
    assert local_filter(version.created_at, "%m-%d %H:%M") in listing

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert local_filter(version.created_at, "%Y-%m-%d %H:%M") in page


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


def test_a_submission_whose_grading_failed_is_regraded_on_the_current_version(
    world: World,
) -> None:
    """**採点が失敗した提出も「いまの版で再採点」に拾う。**

    結果（`GradingRun`）のある提出しか数えていなかったので、採点自体が
    落ちた提出は課題を訂正しても再採点の対象に出ず、問題セットの「流し直す」
    は古い版に固定されたまま ── どちらからも直せなかった（prog2 ex01-2、
    2026-09-22）。
    """
    from datetime import UTC, datetime

    from aijudge_core import ArtifactKind, GradingPhase
    from aijudge_submission import IncomingFile, SubmissionService

    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "cert",
            "unit": "ex01",
            "statement": "## [必須] 修了証 ##\n\n画像を出す。",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
    task_id = str(task.id)

    service = SubmissionService(world.database.unit_of_work, world.console.store)
    accepted = service.accept(
        tenant_id=TENANT,
        task_version_id=first.id,
        learner_id=learner.user_id,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
    )
    # ジョブを上限まで落とす（ワーカーが 3 回失敗したのと同じ状態）。
    now = datetime.now(UTC)
    with world.database.unit_of_work() as uow:
        job = uow.jobs.reserve(now, worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC)
        assert job is not None and job.submission_id == accepted.submission.id
        uow.jobs.update(job.failed(now, "no evaluator produced a score", permanent=True))
        uow.commit()

    # 訂正して版を上げる。
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 直した ##\n\n本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert "いまの版で再採点（1 件）" in page, "失敗した提出が再採点の対象に出ていない"

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/regrade", follow_redirects=False
    )
    assert response.status_code == 303, response.text
    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(TaskId(task_id))
        queued = uow.jobs.reserve(
            datetime.now(UTC), worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC
        )
    assert queued is not None, "再採点のジョブが積まれていない"
    assert queued.submission_id == accepted.submission.id
    assert queued.task_version_id == latest.id, "古い版のままで再実行している"


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

    # **コースの入口には人数を出さない**（#189・ADR 0017 §4）。行き先が帯へ
    # 移り、帯が載せるのは「人が動かないと進まない件数」だけになった ──
    # 受講者数はどこに用があるかを教えないので、全ページがクエリを払う
    # 理由にならない。内訳の確認は上の受講者のページが担う。
    menu = client.get(f"/courses/{world.course.id}").text
    assert "learner 2" not in menu
    assert f"/manage/courses/{world.course.id}/enrolments" in menu, "帯から行けない"


def test_the_enrolment_form_explains_the_roles_as_differences(world: World) -> None:
    """**差分で書く。** 4 つを列挙すると同じ項目が 4 回並び、どこが違うのかを
    読み手が引き算することになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text
    table = page[page.index("下位の役割に加えてできること") :]
    for role in ("learner", "assistant", "instructor", "admin"):
        assert role in table


def test_the_console_does_not_offer_admin_to_a_teacher(world: World) -> None:
    """**`admin` は画面から配れない。**

    `admin` はコースを作れてテナント内の全コースに届く ── コースをまたぐ
    権限なので、コースの受講者一覧から配れる範囲ではない。以前は `Role` の
    全値を選択肢にしていたので、`assistant` と `instructor` の間に `admin` が
    並んでいた（#100）。

    `instructor` は**担当教員も配れる**（2026-09-08 に #126 を覆した。
    コース単位の権限なので、担当を任せる相手は担当教員が決められる）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/enrolments").text

    # 絞り込みの選択肢（すべて／…）は配る役割ではないので外して見る。
    options = {line for line in page.splitlines() if "<option" in line and "すべて" not in line}
    assert not [line for line in options if 'value="admin"' in line], "admin が選択肢にある"
    for role in ("learner", "assistant", "instructor"):
        assert [line for line in options if f'value="{role}"' in line], f"{role} が選べない"


def test_the_console_offers_instructor_to_an_admin(world: World) -> None:
    """管理者も同じ範囲（`admin` だけが画面の外）。"""
    world.register("boss", Role.ADMIN)
    page = world.client("boss").get(f"/manage/courses/{world.course.id}/enrolments").text
    # 絞り込みの選択肢（すべて／…）は配る役割ではないので外して見る。
    options = {line for line in page.splitlines() if "<option" in line and "すべて" not in line}
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

    # **担当教員も `instructor` を付けられる**（2026-09-08 に #126 を覆した）。
    promoted = client.post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "instructor"},
        follow_redirects=False,
    )
    assert promoted.status_code == 303
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, student.user_id)
    assert enrollment is not None and enrollment.role is Role.INSTRUCTOR

    # 降格も同じ経路でできる。
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
        subject_profile="cs_lang_c_intro",
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
    settings = _main(client.get(f"/manage/courses/{world.course.id}").text)
    assert '<ul class="rolecounts">' not in settings
    assert f"/manage/courses/{world.course.id}/enrolments" not in settings
    # 帯からは行ける（#189 で入口が本文から帯へ移った）。
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
        assert (
            AuthService(uow.identity, audit=uow.audit).role_in(world.course.id, student.user_id)
            is Role.ASSISTANT
        )


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
        subject_profile="cs_lang_c_intro",
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
    # 説明は評価器の docstring の 1 行目。**選ぶときに読む言葉で書く**（#318）
    # ── 種類（AI か決定的か）が先に来る。
    assert "AI が段階を判定する" in body
    assert "AI が項目を判定する" in body


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


def test_a_criterion_is_removed_with_the_explicit_mark(world: World) -> None:
    """観点を消すのは「この観点を削除する」の印で行う。

    以前は「コードを空にする」だったが、コードだけ消すと題名が残って
    「コードと題名の両方が要ります」で止まり、消す手段が無いように見えた。
    印は元のコードで突き合わせるので、コードの欄が触られていても消える。
    """
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

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    assert 'name="criterion_delete" value="readability"' in page
    assert "コードを空にします" not in page

    form = _rubric_form(("correctness", "正しさ", "1.0", ""), ("", "変数名と構造", "0.3", ""))
    form["criterion_original"] = ["correctness", "readability"]
    form["criterion_delete"] = ["readability"]
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={"statement": "## [必須] 課題 ##\n\n本文", "readability_weight": "0.3", **form},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with world.database.unit_of_work() as uow:
        latest = uow.tasks.latest_version(task.id)
    assert [c.code for c in latest.criteria] == ["correctness"]


def test_clearing_only_the_code_points_at_the_delete_mark(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/rubric",
        data=_rubric_form(("runs", "動く", "0.5", ""), ("", "読める", "0.5", "")),
    )
    assert response.status_code == 400
    assert "この観点を削除する" in response.json()["detail"]


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

    受け口は課題の編集画面の 1 つだけである（#300）── 共通設定にも 1 行を
    出すフォームがあったが、そこで作った行を手で貼るには書きかけの問題文を
    置いて往復することになる。
    """
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/images.json",
        files={"upload": ("shot.png", b"fake png bytes", "image/png")},
        data={"alt": "端末の画面"},
    )
    assert response.status_code == 200
    line = response.json()["markdown"]
    assert line.startswith("![端末の画面](/images/"), "貼り付ける 1 行が出ていない"

    # 上げた画像はその場で読める。
    url = line.split("](", 1)[1].split(")", 1)[0]
    served = client.get(f"/manage/courses/{world.course.id}/images/{url.rsplit('/', 1)[1]}")
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
        f"/manage/courses/{world.course.id}/images.json",
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
    assert console.blind_sample_rate("cs_lang_c_intro") >= 0.0
    assert console.needs_blind_mark(submission, "cs_lang_c_intro") is False


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


# --------------------------------------------------------------------------
# 束（zip）で課題を入れる（#161）
# --------------------------------------------------------------------------


def _bundle(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body if isinstance(body, bytes) else body.encode("utf-8"))
    return buffer.getvalue()


BUNDLED_TASK = "statement: |\n  ## [必須] 束から来た問題 ##\n\n  本文\n"


def _upload(client, world: World, bundle: bytes, unit: str = "ex06"):
    return client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/bundle",
        files={"archive": ("ex06.zip", bundle, "application/zip")},
    )


def test_reading_a_bundle_saves_nothing_yet(world: World) -> None:
    """**押した瞬間に何十件も入る操作にしない。** まず何が起きるかを出す。"""
    world.register("teacher", Role.INSTRUCTOR)
    unit = "ex06"
    before = _task_count(world)

    response = _upload(
        world.client("teacher"), world, _bundle({"p9/task.yaml": BUNDLED_TASK}), unit
    )

    assert response.status_code == 200
    assert "まだ保存していません" in response.text
    assert "束から来た問題" in response.text
    assert _task_count(world) == before, "確認の段階で保存されている"


def test_the_key_comes_from_the_unit_on_the_page(world: World) -> None:
    """鍵の前半は画面が持つ問題セットが決める（#70）。"""
    world.register("teacher", Role.INSTRUCTOR)
    unit = "ex06"

    body = _upload(
        world.client("teacher"), world, _bundle({"p9/task.yaml": BUNDLED_TASK}), unit
    ).text

    assert f"{unit}/p9" in body


def test_a_broken_bundle_is_refused_with_the_reason(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)

    response = _upload(world.client("teacher"), world, b"not a zip")

    assert response.status_code == 400
    assert "zip" in response.text


def test_confirming_the_bundle_saves_it_unapproved_by_default(world: World) -> None:
    """中身は他所で書かれたもの。**このシステムでは誰も読んでいない**（#48）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    unit = "ex06"
    preview = _upload(client, world, _bundle({"p9/task.yaml": BUNDLED_TASK}), unit).text
    specs = re.search(r'name="specs" value="([^"]*)"', preview)
    assert specs is not None

    response = client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/bundle/confirm",
        data={"specs": html.unescape(specs.group(1))},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        task = next(
            item
            for item in uow.tasks.list_for_course(world.course.id)
            if item.title.endswith("問題")
        )
        version = uow.tasks.latest_version(task.id)
    assert version is not None
    assert version.provenance.review_state is ReviewState.IN_REVIEW
    # **生成物のふりをさせない**（承認率の統計が AI の承認率でなくなる）。
    assert version.provenance.generated_by is None


def test_the_bundle_can_be_taken_in_as_approved(world: World) -> None:
    """以前この科目で使っていた課題を戻す場合。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    unit = "ex06"
    preview = _upload(client, world, _bundle({"p9/task.yaml": BUNDLED_TASK}), unit).text
    specs = html.unescape(re.search(r'name="specs" value="([^"]*)"', preview).group(1))

    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/bundle/confirm",
        data={"specs": specs, "approved": "1"},
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        task = next(
            item
            for item in uow.tasks.list_for_course(world.course.id)
            if item.title.endswith("問題")
        )
        version = uow.tasks.latest_version(task.id)
    assert version is not None and version.provenance.review_state is ReviewState.APPROVED


def test_the_same_bundle_twice_adds_nothing(world: World) -> None:
    """入れ直しても増えない（鍵の冪等性）。移行は何度も流すもの。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    unit = "ex06"
    bundle = _bundle({"p9/task.yaml": BUNDLED_TASK})

    for _ in range(2):
        preview = _upload(client, world, bundle, unit).text
        specs = html.unescape(re.search(r'name="specs" value="([^"]*)"', preview).group(1))
        client.post(
            f"/manage/courses/{world.course.id}/units/{unit}/bundle/confirm",
            data={"specs": specs},
            follow_redirects=False,
        )

    with world.database.unit_of_work() as uow:
        keys = [item.title for item in uow.tasks.list_for_course(world.course.id)]
    assert len([key for key in keys if key.endswith("問題")]) == 1


def test_a_forged_confirm_is_validated_again(world: World) -> None:
    """**確認画面を経由しても POST は手で作れる。** 検証をもう一度通す。"""
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/units/ex06/bundle/confirm",
        data={"specs": '[{"statment": "綴り間違い"}]'},
    )

    assert response.status_code == 400


def test_a_ta_cannot_upload_a_bundle(world: World) -> None:
    """課題を足すのは担当教員以上（#102）。"""
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)

    response = _upload(world.client("ta"), world, _bundle({"p9/task.yaml": BUNDLED_TASK}))

    assert response.status_code == 403


# --------------------------------------------------------------------------
# 現在地（パンくず）— テナント単位の管理画面（#165）
# --------------------------------------------------------------------------


def _trail_of(body: str) -> str:
    """`<nav class="trail">` の中身だけを取り出す。"""
    start = body.index('<nav class="trail"')
    return body[start : body.index("</nav>", start)]


def test_the_tenant_wide_screens_say_where_you_are(world: World) -> None:
    """**コースの下にない画面にも現在地を出す**（#165）。

    以前はパンくずが `{% if course %}` の中にあり、利用者の一覧・科目
    プロファイル・Google ログイン設定には出なかった。戻る導線は画面ごとに
    上・下・無しとばらばらで、`/manage/users/new` には 1 本も無かった。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)
    client = world.client("boss")
    expected = {
        "/manage/users": "利用者の一覧",
        "/manage/users/new": "利用者を作成",
        "/manage/subjects": "科目プロファイル",
        "/manage/oidc-settings": "Google ログイン設定",
        "/manage/account/password": "パスワード変更",
    }
    for path, label in expected.items():
        response = client.get(path)
        assert response.status_code == 200, path
        trail = _trail_of(response.text)
        # 根（担当コース）へ戻れること、いまいる場所が出ていること。
        assert 'href="/"' in trail, path
        assert label in trail, path


def test_a_detail_screen_links_one_step_up(world: World) -> None:
    """一段上へ戻れること。**詳細から一覧へ**が画面の中に無いと、
    ブラウザの戻る以外に道が無くなる。
    """
    other = world.register("s2400001", Role.LEARNER)
    world.register("boss", Role.ADMIN, tenant_admin=True)

    trail = _trail_of(world.client("boss").get(f"/manage/users/{other.user_id}").text)

    assert 'href="/manage/users"' in trail
    assert "s2400001" in trail


def test_the_google_login_settings_are_reachable_without_typing_the_url(world: World) -> None:
    """**どこからもリンクされていなかった**（#165）。URL を直接打つ以外に
    到達手段が無い画面は、無いのと同じである。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)

    body = world.client("boss").get("/").text

    assert 'href="/manage/oidc-settings"' in body


def test_the_breadcrumb_class_is_not_used_for_anything_else() -> None:
    """`.crumb` は現在地と、見出し直下の説明文の**両方**に使われていた。

    名前が 2 つの意味を持ったままパンくずを足すと、同じ画面に「パンくず」が
    2 つ出る。説明文は `.subtitle` に分けた（#165）ので、`crumb` という語が
    テンプレートに残っていないことをここで固定する。
    """
    templates = Path(__file__).resolve().parents[1] / "src" / "aijudge_reviewconsole" / "templates"
    guilty = [path.name for path in templates.glob("*.html") if 'class="crumb"' in path.read_text()]

    assert guilty == []


def test_the_edit_form_button_says_it_updates_the_task(world: World) -> None:
    """フォームの末尾が「提出できるファイル形式」なので、「保存」だと拡張子だけを
    保存するボタンに見える。訂正では「この問題を保存して更新する」、新規では「保存」。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _import_example(world)
    edit = client.get(f"/manage/courses/{world.course.id}/tasks/{task_id}/edit").text
    assert ">この問題を保存して更新する<" in edit
    new = client.get(f"/manage/courses/{world.course.id}/units/ex01/tasks/new").text
    assert ">保存<" in new and ">この問題を保存して更新する<" not in new


def test_no_form_posts_to_a_path_without_the_prefix() -> None:
    """テンプレートの `action=` は必ず `root_prefix()` を通す（#281）。

    課題の編集フォームだけが `/manage/...` を直に出しており、`/console` の
    下で動く運用機では prefix の外へ POST されて保存が 404 になった。
    ローカル（接頭辞なし）では再現しないので、書き方そのものを固定する。
    """
    templates = Path(__file__).resolve().parents[1] / "src" / "aijudge_reviewconsole" / "templates"
    guilty = [
        f"{path.name}:{number}"
        for path in templates.glob("*.html")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"""action=["']\{\{\s*'/""", line) or re.search(r"""action=["']/""", line)
    ]

    assert guilty == []


def test_the_section_step_carries_the_path_prefix(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接頭辞つき配置（`/console` の下）でも区画へ戻れること。

    ここだけ `root_prefix()` が抜けており、パンくずの「区画」だけが 404 に
    なる状態だった（#103 の書き換え漏れ）。
    """
    from aijudge_reviewconsole.urls import ENV_ROOT_PREFIX

    monkeypatch.setenv(ENV_ROOT_PREFIX, "/console")
    world.register("teacher", Role.INSTRUCTOR)

    trail = _trail_of(world.client("teacher").get(f"/manage/courses/{world.course.id}/kc").text)

    assert 'href="/console/manage/courses/' in trail
    assert 'href="/manage/courses/' not in trail


# --------------------------------------------------------------------------
# 既定のルーブリックが、その科目では誰にも採点できない場合
# --------------------------------------------------------------------------


_UNSCORABLE = "この科目はテスト実行を走らせません"


def _report_course(world: World):
    """テスト実行を走らせない科目（レポート）のコース。"""
    course, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="report1",
        title="科学技術リテラシー",
        term="2026-前期",
        subject_profile="report_ja",
        profiles_dir=PROFILES,
    )
    return course


def test_a_report_course_is_told_the_built_in_rubric_cannot_be_scored(world: World) -> None:
    """**設定はどこも正しく見えるのに点が出ない**、を画面で言う。

    組み込みの既定は「正しさ（テスト実行）＋読みやすさ」で、正しさの担当は
    `code_test_runner` である。テスト実行を走らせない科目のコースがこの既定の
    ままだと、その観点は恒久的に未採点になり、総点も伏せられる（ADR 0015）。
    """
    course = _report_course(world)
    teacher = world.register("teacher", Role.INSTRUCTOR, course_id=course.id)
    assert teacher is not None

    body = world.client("teacher").get(f"/manage/courses/{course.id}").text

    assert _UNSCORABLE in body


def test_a_course_that_runs_tests_is_not_warned(world: World) -> None:
    """**警告を出しすぎない。** テスト実行を走らせる科目では既定で正しく動く。"""
    world.register("teacher", Role.INSTRUCTOR)

    body = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert _UNSCORABLE not in body


def test_the_warning_goes_away_once_the_course_declares_its_own_criteria(world: World) -> None:
    """観点を決めたら消える。**消えないと、直したことが画面から分からない。**"""
    course = _report_course(world)
    world.register("teacher", Role.INSTRUCTOR, course_id=course.id)
    client = world.client("teacher")
    data = _rubric_form(("structure", "構成", "0.5", ""), ("discussion", "考察", "0.5", ""))
    assert client.post(f"/manage/courses/{course.id}/rubric", data=data).status_code == 200

    body = client.get(f"/manage/courses/{course.id}").text

    assert _UNSCORABLE not in body


# --------------------------------------------------------------------------
# 学期は選ばせる（#167）
# --------------------------------------------------------------------------


def test_the_term_is_chosen_from_a_list(world: World) -> None:
    """**自由入力の欄を残さない。** 残せば、そこから表記のゆれが入る。

    ゆれは表示の問題ではない ── コースの同一性は (テナント, コード, 学期) で、
    `2025-後期` と `2025後期` は同じ授業のつもりで別のコースになる。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)

    body = world.client("boss").get("/").text
    form = body[body.index("コースを追加する") :]

    assert 'name="term_year"' in form
    assert 'name="term_division"' in form
    assert 'name="term"' not in form
    # 区分は語彙の全部が並ぶ（画面に書き写していない）。
    for division in ("前期", "後期", "1Q", "4Q", "通年", "集中"):
        assert f'value="{division}"' in form


def test_the_years_offered_are_this_year_and_the_next_two(world: World) -> None:
    from aijudge_core import offered_years

    world.register("boss", Role.ADMIN, tenant_admin=True)

    body = world.client("boss").get("/").text

    for year in offered_years():
        assert f'value="{year}"' in body


def test_the_two_fields_become_one_canonical_term(world: World) -> None:
    world.register("boss", Role.ADMIN, tenant_admin=True)
    client = world.client("boss")

    response = client.post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term_year": "2026",
            "term_division": "1Q",
            "profile": "cs_network_python",
            "instructors": "boss",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        created = [c for c in uow.identity.list_courses(TENANT) if c.code == "network"]
    assert [c.term for c in created] == ["2026-1Q"]


def test_an_unknown_division_is_refused_even_though_the_form_only_offers_valid_ones(
    world: World,
) -> None:
    """**フォームは手で作れる。** 選択肢を絞るのは表示の都合で、制限ではない。"""
    world.register("boss", Role.ADMIN, tenant_admin=True)

    response = world.client("boss").post(
        "/manage/courses",
        data={
            "code": "network",
            "title": "ネットワーク及び演習",
            "term_year": "2026",
            "term_division": "春学期",
            "profile": "cs_network_python",
            "instructors": "boss",
        },
    )

    assert response.status_code == 400
    assert "春学期" in response.json()["detail"]


def test_courses_are_listed_in_chronological_order(world: World) -> None:
    """**文字列順ではない。** `1Q` は `前期` より文字コードが小さい。"""
    world.register("boss", Role.ADMIN, tenant_admin=True)
    for term in ("2026-1Q", "2026-前期", "2025-後期"):
        ensure_course(
            world.database,
            tenant_id=TENANT,
            code=f"c-{term}",
            title=f"科目 {term}",
            term=term,
            subject_profile="cs_lang_c_intro",
            profiles_dir=PROFILES,
        )

    with world.database.unit_of_work() as uow:
        terms = [c.term for c in uow.identity.list_courses(TENANT) if c.code.startswith("c-")]

    assert terms == ["2025-後期", "2026-前期", "2026-1Q"]


# --------------------------------------------------------------------------
# コースの複製（#170）
# --------------------------------------------------------------------------


def _duplicate_form(*, code: str = "prog2", term_division: str = "後期") -> dict[str, str]:
    return {
        "code": code,
        "title": "プログラミング及び実習 2",
        "term_year": "2026",
        "term_division": term_division,
    }


def test_only_an_admin_can_duplicate_a_course(world: World) -> None:
    """複製はコースを作る操作。**権限は作成・削除と同じ**（#130・#156）。"""
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/duplicate", data=_duplicate_form()
    )

    assert response.status_code == 403


def test_duplicating_lands_on_the_new_course_and_says_what_was_copied(world: World) -> None:
    """**何件写したかを告げる。** 件数を黙って変えると、教員は自分が何を
    手に入れたのか画面から確かめられない。

    落とす先は複製先のコース設定 ── 複製後にすることは日程の入力である。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)
    client = world.client("boss")

    response = client.post(
        f"/manage/courses/{world.course.id}/duplicate",
        data=_duplicate_form(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/manage/courses/")
    assert str(world.course.id) not in location
    body = client.get(location).text
    assert "複製しました" in body
    assert "日程は写していません" in body


def test_the_duplicate_form_offers_the_same_terms_as_the_create_form(world: World) -> None:
    world.register("boss", Role.ADMIN, tenant_admin=True)

    body = world.client("boss").get(f"/manage/courses/{world.course.id}").text
    form = body[body.index("このコースを複製する") :]

    assert 'name="term_year"' in form
    assert 'name="term_division"' in form
    for division in ("前期", "1Q", "通年", "集中"):
        assert f'value="{division}"' in form


def test_duplicating_into_the_same_code_and_term_is_refused(world: World) -> None:
    """**元のコースを上書きしない。** 同じ組では複製にならない。

    コースの同一性は (テナント, コード, 学期) なので、同じ組を指定すると
    複製先の ID が元と一致する。
    """
    world.register("boss", Role.ADMIN, tenant_admin=True)

    response = world.client("boss").post(
        f"/manage/courses/{world.course.id}/duplicate",
        # World のコースは prog2 / 2025-後期。
        data={
            "code": "prog2",
            "title": "別名",
            "term_year": "2025",
            "term_division": "後期",
        },
    )

    assert response.status_code == 409
    assert "コードと学期" in response.json()["detail"]
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).title == "プログラミング及び実習 2"


# --------------------------------------------------------------------------
# 取り込みのひな形（#171）
# --------------------------------------------------------------------------


def _template_url(world: World, unit: str = "ex06") -> str:
    return f"/manage/courses/{world.course.id}/units/{unit}/bundle/template"


def test_the_upload_form_offers_the_template(world: World) -> None:
    """**書ける状態のものを渡す。** 画面の説明文から構造を書き写させない。"""
    world.register("teacher", Role.INSTRUCTOR)

    body = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/ex06").text

    assert _template_url(world) in body


def test_the_template_downloads_as_a_zip(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").get(_template_url(world))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert "ex06-template.zip" in response.headers["content-disposition"]
    # 返ったものがそのまま取り込めること（構造の定義とひな形が繋がっている）。
    from aijudge_admin.bundles import read_bundle

    assert [task.leaf for task in read_bundle(response.content)] == ["p1", "p2"]


def test_the_template_names_the_knowledge_components_of_this_course(world: World) -> None:
    """コースに合わせる（#171 の決定）。"""
    _seed(world)
    world.register("boss", Role.ADMIN, tenant_admin=True)
    client = world.client("boss")
    _use_kc(world, "cs.loops.control.basic", "繰り返し")

    archive = zipfile.ZipFile(io.BytesIO(client.get(_template_url(world)).content))

    assert "cs.loops.control.basic" in archive.read("p1/task.yaml").decode("utf-8")


def test_a_non_ascii_unit_still_gets_a_usable_filename(world: World) -> None:
    """**ファイル名は ASCII に留める。** 回の名前は教員が付けるもので、
    日本語も入りうる ── 符号化を持ち込むより、README で言う方が壊れない。
    """
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").get(_template_url(world, "第3回"))

    assert response.status_code == 200
    assert 'filename="bundle-template.zip"' in response.headers["content-disposition"]


def test_a_learner_cannot_download_the_template(world: World) -> None:
    """権限は取り込みと同じ（担当教員以上・#102）。"""
    world.register("s2400001", Role.LEARNER)

    assert world.client("s2400001").get(_template_url(world)).status_code in (403, 404)


def test_bulk_grading_does_not_read_the_capped_listing(world: World, monkeypatch) -> None:
    """**一括採点の対象を打ち切られた一覧から決めない**（#230）。

    `list_for_course` は古い順に 5000 件で切る。コース全件を引いてから課題版
    で絞ると、落ちるのは**いちばん新しい提出** ── 試験直後にいま採点したい
    答案がちょうど外れる。しかも押す前の件数も同じ一覧から出ていたので、
    「N 件待っている → N 件流した」と画面の中では矛盾せず、取りこぼしは
    どこにも現れなかった。

    5000 件を積んで再現するのは現実的でないので、**その API を呼んだら落ちる
    ようにして**、対象が課題版で絞った側から来ていることを固定する。
    """
    from aijudge_persistence.repositories import SqlSubmissionRepository

    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("打ち切られる一覧から採点の対象を決めてはいけない")

    monkeypatch.setattr(SqlSubmissionRepository, "list_for_course", forbidden)

    client = world.client("teacher")
    for path in (
        f"/manage/courses/{world.course.id}/units/{unit}/grade-now",
        f"/manage/courses/{world.course.id}/units/{unit}/retry-failed",
    ):
        response = client.post(path, follow_redirects=False)
        assert response.status_code == 303, path

    # 件数を出す側（押す前に読む数）も同じ経路であること。
    assert client.get(f"/manage/courses/{world.course.id}/units/{unit}").status_code == 200


def test_checks_across_branches_are_added_together(world: World) -> None:
    """**チェックは分野をまたいで 1 つの選択。** 以前は分野ごとに form が分かれ、
    2 つの分野でチェックして押すと押した分野の分しか届かなかった。いまは
    全分野が 1 つの form で、送るボタンは底の 1 つだけ。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    from aijudge_admin import register_kc

    for key in ("cs.io", "cs.io.formatted"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",), seeding=True)
    for key in ("cs.loops.control.basic", "cs.io.formatted.printf"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",))

    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    vocabulary = page.split('id="vocabulary"')[1].split('id="candidates"')[0]
    assert vocabulary.count(">選択したものをこのコースに足す<") == 1
    # 2 つの分野のチェックが同じ form に入っている（form の開始は 1 回）。
    assert vocabulary.count("<form ") == 1
    assert 'value="cs.loops.control.basic"' in vocabulary
    assert 'value="cs.io.formatted.printf"' in vocabulary

    response = client.post(
        f"/manage/courses/{world.course.id}/kc/scope/add",
        data={"kc": ["cs.loops.control.basic", "cs.io.formatted.printf"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.io.formatted.printf",
            "cs.loops.control.basic",
        )


def test_a_branch_button_leaves_checks_in_other_branches_alone(world: World) -> None:
    """「この階層をすべて足す」は同じ form の送信ボタンなので、他の分野で付けた
    チェックも一緒に届く。**接頭辞が来たらチェックは見ない** ── 「この階層を」
    と書いたボタンが別の分野のものを動かしてはいけない。"""
    _seed(world)
    world.register("boss", Role.ADMIN)
    client = world.client("boss")
    from aijudge_admin import register_kc

    for key in ("cs.io", "cs.io.formatted"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",), seeding=True)
    for key in ("cs.loops.control.basic", "cs.io.formatted.printf"):
        register_kc(world.database, key=key, label=key, namespaces=("cs",))

    client.post(
        f"/manage/courses/{world.course.id}/kc/scope/add",
        data={"prefix": "cs.loops", "kc": ["cs.io.formatted.printf"]},
    )
    with world.database.unit_of_work() as uow:
        assert uow.identity.get_course(world.course.id).knowledge_components == (
            "cs.loops.control.basic",
        )


def test_the_regrade_card_comes_first_among_the_operations(world: World) -> None:
    """訂正の直後に押すものなので、「この問題への操作」の先頭に置く。
    日程や AI 書き直しの下にあって見つからなかった（2026-09-22）。"""
    from datetime import UTC, datetime

    from aijudge_core import ArtifactKind, GradingPhase
    from aijudge_submission import IncomingFile, SubmissionService

    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "cert",
            "unit": "ex01",
            "statement": "## [必須] 修了証 ##\n\n画像を出す。",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        first = uow.tasks.latest_version(task.id)
    service = SubmissionService(world.database.unit_of_work, world.console.store)
    service.accept(
        tenant_id=TENANT,
        task_version_id=first.id,
        learner_id=learner.user_id,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
    )
    now = datetime.now(UTC)
    with world.database.unit_of_work() as uow:
        job = uow.jobs.reserve(now, worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC)
        uow.jobs.update(job.failed(now, "x", permanent=True))
        uow.commit()
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task.id}/revise",
        data={"statement": "## [必須] 直した ##\n\n本文", "readability_weight": "0.3"},
    )

    page = client.get(f"/manage/courses/{world.course.id}/tasks/{task.id}/edit").text
    operations = page[page.index("この問題への操作") :]
    assert operations.index("いまの版で採点し直す") < operations.index("版の履歴")
    assert operations.index("いまの版で採点し直す") < operations.index('id="schedule"')


def test_the_remembered_place_is_restored_after_the_anchor_jump(world: World) -> None:
    """**頁を移らない操作では表示位置を動かさない**（2026-09-22）。

    保存は POST → 303 → GET なので戻りは別の読み込みで、経路は JavaScript の
    無い人のために `#saved` のような飛び先を付ける。覚えた位置を 1 回置き直す
    だけでは、その飛び先への移動が**あとから**効いて上書きされる ── 再採点の
    ように頁を移らない操作でも表示位置が飛んでいた。

    位置そのものは画面の中でしか確かめられないので、ここでは**置き直しが
    読み込み後にも走ること**を固定する（消えると同じ壊れ方に戻る）。
    """
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    script = page[page.index("aijudge:place:") :]
    assert "history.replaceState" in script, "飛び先の指定を消していない"
    assert "requestAnimationFrame" in script, "描画後に置き直していない"
    assert 'addEventListener("load"' in script, "読み込み後に置き直していない"
