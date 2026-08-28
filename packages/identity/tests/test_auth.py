"""認証の規則を固定する（S1）。

固定したいのは「漏らさない」ことが中心。学生が触る画面の手前にある層なので、
ここが緩いと他の設計原則がすべて無意味になる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import Course, Role
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_identity import (
    AuthenticationFailed,
    AuthService,
    InMemoryIdentityRepository,
    PermissionDenied,
    WeakPassword,
    hash_password,
    needs_rehash,
    verify_password,
)

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)

PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, hours: float) -> None:
        self.now += timedelta(hours=hours)


@pytest.fixture
def auth() -> tuple[AuthService, InMemoryIdentityRepository, Clock]:
    repository = InMemoryIdentityRepository()
    clock = Clock()
    return AuthService(repository, clock=clock), repository, clock


def register(service: AuthService, login: str = "s2400001") -> object:
    return service.register(tenant_id=TENANT, login=login, display_name="学生 A", password=PASSWORD)


# --------------------------------------------------------------------------
# パスワード
# --------------------------------------------------------------------------


def test_the_password_is_not_recoverable_from_the_hash() -> None:
    encoded = hash_password(PASSWORD)
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong password", encoded)


def test_two_hashes_of_the_same_password_differ() -> None:
    """ソルトが効いていること。同じなら一覧から共通パスワードが割れる。"""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_the_parameters_are_stored_with_the_hash() -> None:
    """埋め込まないと、パラメータを上げた日に全員ログインできなくなる。"""
    encoded = hash_password(PASSWORD, n=1024)
    assert "n=1024" in encoded
    assert verify_password(PASSWORD, encoded)


def test_a_weak_hash_is_flagged_for_rehashing() -> None:
    assert needs_rehash(hash_password(PASSWORD, n=1024))
    assert not needs_rehash(hash_password(PASSWORD))


def test_a_malformed_hash_verifies_as_false_not_an_exception() -> None:
    """例外にすると、壊れたハッシュを持つ利用者だけ 500 になり観測できる。"""
    for broken in ("", "garbage", "scrypt$bad$x$y", "argon2$n=1$a$b"):
        assert verify_password(PASSWORD, broken) is False


def test_a_short_password_is_refused() -> None:
    with pytest.raises(WeakPassword):
        hash_password("short")


# --------------------------------------------------------------------------
# ログイン
# --------------------------------------------------------------------------


def test_a_registered_user_can_log_in(auth) -> None:
    service, _, _ = auth
    principal = register(service)
    logged_in, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    assert logged_in.user_id == principal.user_id
    assert token


def test_the_principal_carries_no_credentials(auth) -> None:
    """テンプレートやログにパスワードハッシュが漏れる経路を作らない。"""
    service, _, _ = auth
    principal = register(service)
    assert "password_hash" not in principal.model_dump()


def test_a_wrong_password_is_refused(auth) -> None:
    service, _, _ = auth
    register(service)
    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=TENANT, login="s2400001", password="wrong password")


def test_an_unknown_login_gives_the_same_message_as_a_wrong_password(auth) -> None:
    """理由を分けると、有効な ID の一覧を作れてしまう。"""
    service, _, _ = auth
    register(service)
    with pytest.raises(AuthenticationFailed) as unknown:
        service.login(tenant_id=TENANT, login="nobody", password=PASSWORD)
    with pytest.raises(AuthenticationFailed) as wrong:
        service.login(tenant_id=TENANT, login="s2400001", password="wrong password")
    assert str(unknown.value) == str(wrong.value)


def test_a_login_from_another_tenant_does_not_match(auth) -> None:
    """テナント境界。Phase 8 で機関が増えたときに効く。"""
    service, _, _ = auth
    register(service)
    other = TenantId("ten_" + "9" * 32)
    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=other, login="s2400001", password=PASSWORD)


def test_a_duplicate_login_is_refused(auth) -> None:
    service, _, _ = auth
    register(service)
    with pytest.raises(AuthenticationFailed, match="既に使われて"):
        register(service)


# --------------------------------------------------------------------------
# セッション
# --------------------------------------------------------------------------


def test_the_token_is_not_stored_in_the_clear(auth) -> None:
    """DB が漏れてもセッションを乗っ取れないようにする。"""
    service, repository, _ = auth
    register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)

    stored = list(repository._sessions.values())
    assert len(stored) == 1
    assert token not in stored[0].token_hash
    assert stored[0].token_hash.startswith("sha256:")


def test_a_token_resolves_to_its_principal(auth) -> None:
    service, _, _ = auth
    principal = register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    resolved = service.resolve(token)
    assert resolved is not None
    assert resolved.user_id == principal.user_id


def test_an_unknown_token_resolves_to_nothing(auth) -> None:
    service, _, _ = auth
    assert service.resolve("not-a-real-token") is None
    assert service.resolve("") is None


def test_an_expired_session_stops_working(auth) -> None:
    service, _, clock = auth
    register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    clock.advance(13)
    assert service.resolve(token) is None


def test_logging_out_revokes_the_session(auth) -> None:
    service, _, _ = auth
    register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    service.logout(token)
    assert service.resolve(token) is None


def test_changing_the_password_cuts_existing_sessions(auth) -> None:
    """乗っ取られていた場合の復旧手段がこれしかない。"""
    service, _, _ = auth
    principal = register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    service.change_password(principal.user_id, current=PASSWORD, new="a new long password")
    assert service.resolve(token) is None


def test_changing_the_password_needs_the_current_one(auth) -> None:
    service, _, _ = auth
    principal = register(service)
    with pytest.raises(AuthenticationFailed):
        service.change_password(principal.user_id, current="wrong", new="another long password")


def test_disabling_a_user_cuts_their_sessions_without_deleting_them(auth) -> None:
    """消すと過去の提出と採点の参照が壊れる。"""
    service, repository, _ = auth
    principal = register(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    service.disable(principal.user_id)

    assert service.resolve(token) is None
    assert repository.get_user(principal.user_id) is not None
    with pytest.raises(AuthenticationFailed, match="無効化"):
        service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)


# --------------------------------------------------------------------------
# コースと権限
# --------------------------------------------------------------------------


def _course(service: AuthService, repository: InMemoryIdentityRepository) -> Course:
    course = Course(
        id=COURSE,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
    )
    repository.save_course(course)
    return course


def test_a_learner_is_a_member_but_cannot_grade(auth) -> None:
    service, repository, _ = auth
    principal = register(service)
    _course(service, repository)
    service.enroll(tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=Role.LEARNER)

    assert service.require_membership(COURSE, principal.user_id) is Role.LEARNER
    with pytest.raises(PermissionDenied, match="採点の権限"):
        service.require_grader(COURSE, principal.user_id)


def test_an_instructor_can_grade(auth) -> None:
    service, repository, _ = auth
    principal = register(service, "instructor")
    _course(service, repository)
    service.enroll(
        tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=Role.INSTRUCTOR
    )
    assert service.require_grader(COURSE, principal.user_id) is Role.INSTRUCTOR


def test_a_non_member_is_refused(auth) -> None:
    """UI で隠すのは表示の都合であって権限ではない。"""
    service, repository, _ = auth
    principal = register(service)
    _course(service, repository)
    with pytest.raises(PermissionDenied, match="受講者ではありません"):
        service.require_membership(COURSE, principal.user_id)


def test_a_learner_sees_only_their_own_courses(auth) -> None:
    service, repository, _ = auth
    mine = register(service, "mine")
    theirs = register(service, "theirs")
    _course(service, repository)
    other = Course(
        id=CourseId("crs_" + "2" * 32),
        tenant_id=TENANT,
        code="math1",
        title="微分積分",
        term="2026-前期",
        subject_profile="math_calculus",
    )
    repository.save_course(other)
    service.enroll(tenant_id=TENANT, course_id=COURSE, user_id=mine.user_id, role=Role.LEARNER)
    service.enroll(tenant_id=TENANT, course_id=other.id, user_id=theirs.user_id, role=Role.LEARNER)

    courses = service.courses_for(TENANT, mine.user_id)
    assert [course.code for course in courses] == ["prog2"]


def test_an_assistant_can_grade(auth) -> None:
    """複数教員での採点分担（Phase 2）の土台。"""
    service, repository, _ = auth
    principal = register(service, "ta")
    _course(service, repository)
    service.enroll(
        tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=Role.ASSISTANT
    )
    assert service.require_grader(COURSE, principal.user_id) is Role.ASSISTANT


def test_an_unknown_user_id_has_no_role(auth) -> None:
    service, _, _ = auth
    assert service.role_in(COURSE, UserId("usr_" + "f" * 32)) is None


# --------------------------------------------------------------------------
# API トークン — 非対話の呼び出し元
# --------------------------------------------------------------------------


def _service_with_user(login: str = "sano"):
    from aijudge_identity import AuthService, InMemoryIdentityRepository

    service = AuthService(InMemoryIdentityRepository())
    principal = service.register(
        tenant_id=TENANT, login=login, display_name=login, password="correct horse battery"
    )
    return service, principal


def test_an_api_token_resolves_to_its_owner() -> None:
    from aijudge_identity import TOKEN_PREFIX

    service, principal = _service_with_user()
    _record, token = service.issue_token(
        tenant_id=TENANT, user_id=principal.user_id, note="初回移行の流し込み"
    )

    # 接頭辞を付けるのは、ログや issue に貼られた文字列がトークンだと
    # 気づけるようにするため。気づけなければ失効させようがない。
    assert token.startswith(TOKEN_PREFIX)
    resolved = service.resolve_api_token(token)
    assert resolved is not None
    assert resolved.user_id == principal.user_id


def test_the_plaintext_token_is_not_recoverable() -> None:
    """**保存するのはハッシュだけ。** DB が漏れても API を叩けない。"""
    service, principal = _service_with_user()
    record, token = service.issue_token(
        tenant_id=TENANT, user_id=principal.user_id, note="初回移行の流し込み"
    )

    assert token not in record.token_hash
    assert record.token_hash.startswith("sha256:")
    for listed in service.list_tokens(TENANT):
        assert token not in listed.model_dump_json()


def test_a_revoked_token_stops_working() -> None:
    service, principal = _service_with_user()
    record, token = service.issue_token(
        tenant_id=TENANT, user_id=principal.user_id, note="初回移行の流し込み"
    )
    service.revoke_token(record.id)

    assert service.resolve_api_token(token) is None


def test_an_expired_token_stops_working() -> None:
    from datetime import UTC, datetime, timedelta

    from aijudge_identity import AuthService, InMemoryIdentityRepository

    now = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    clock = {"t": now}
    service = AuthService(InMemoryIdentityRepository(), clock=lambda: clock["t"])
    principal = service.register(
        tenant_id=TENANT, login="sano", display_name="sano", password="correct horse battery"
    )
    _record, token = service.issue_token(
        tenant_id=TENANT, user_id=principal.user_id, note="短命トークン", days=1
    )

    assert service.resolve_api_token(token) is not None
    clock["t"] = now + timedelta(days=2)
    assert service.resolve_api_token(token) is None


def test_disabling_the_user_stops_their_tokens() -> None:
    """**権限は利用者に付いている。** トークンに独自の権限を持たせていない。

    持たせると、利用者を無効化したのにトークンだけ生き残る経路ができる。
    """
    service, principal = _service_with_user()
    _record, token = service.issue_token(
        tenant_id=TENANT, user_id=principal.user_id, note="初回移行の流し込み"
    )
    service.disable(principal.user_id)

    assert service.resolve_api_token(token) is None


def test_a_token_needs_a_stated_purpose() -> None:
    """用途の分からないトークンは、消してよいのか判断できないまま残る。"""
    service, principal = _service_with_user()
    for note in ("", "   "):
        with pytest.raises(ValueError):
            service.issue_token(tenant_id=TENANT, user_id=principal.user_id, note=note)


def test_a_session_token_is_not_an_api_token() -> None:
    """入口を分ける。片方が漏れてももう片方にはならない。"""
    service, _principal = _service_with_user()
    _p, session_token = service.login(
        tenant_id=TENANT, login="sano", password="correct horse battery"
    )

    assert service.resolve_api_token(session_token) is None
