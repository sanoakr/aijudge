"""認証の規則が SQL 実装でも同じであることを確かめる。

`packages/identity/tests` はインメモリ実装で規則を固定している。同じ
`AuthService` を SQL の保存先に載せて通すのがここ。片方だけ通る規則は、
移行した日に破綻する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import Course, Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import AuthenticationFailed, AuthService, PermissionDenied
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)
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


class SqlBackedAuth:
    """トランザクションを跨いで認証を使う。

    `AuthService` は 1 リポジトリを持つ設計なので、リクエストごとに
    UnitOfWork を開いてサービスを組み立てる。実際のアプリもこの形になる。
    """

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    def do(self, action):
        with self._database.unit_of_work() as uow:
            service = AuthService(uow.identity, clock=self._clock)
            result = action(service)
            uow.commit()
        return result


@pytest.fixture
def backend():
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    clock = Clock()
    yield SqlBackedAuth(database, clock), clock, database
    database.dispose()


def _register(backend, login: str = "s2400001"):
    return backend.do(
        lambda service: service.register(
            tenant_id=TENANT, login=login, display_name="学生 A", password=PASSWORD
        )
    )


def _login(backend, login: str = "s2400001", password: str = PASSWORD):
    return backend.do(
        lambda service: service.login(tenant_id=TENANT, login=login, password=password)
    )


def test_a_user_can_log_in_across_transactions(backend) -> None:
    auth, _, _ = backend
    principal = _register(auth)
    logged_in, token = _login(auth)
    assert logged_in.user_id == principal.user_id

    resolved = auth.do(lambda service: service.resolve(token))
    assert resolved is not None
    assert resolved.user_id == principal.user_id


def test_the_password_hash_is_not_in_the_principal(backend) -> None:
    auth, _, _ = backend
    principal = _register(auth)
    assert "password_hash" not in principal.model_dump()


def test_the_token_is_stored_hashed(backend) -> None:
    """DB が漏れてもセッションを乗っ取れないこと。"""
    auth, _, database = backend
    _register(auth)
    _, token = _login(auth)

    from aijudge_persistence.schema import SessionRow

    with database.session() as session:
        rows = session.query(SessionRow).all()
    assert len(rows) == 1
    assert token not in rows[0].token_hash


def test_a_duplicate_login_is_refused_by_the_database(backend) -> None:
    """アプリ側の確認だけでは、同時登録が両方通る。"""
    auth, _, _ = backend
    _register(auth)
    with pytest.raises(AuthenticationFailed, match="既に使われて"):
        _register(auth)


def test_the_same_login_in_another_tenant_is_allowed(backend) -> None:
    """機関をまたいで学籍番号が重なるのは普通のこと。"""
    auth, _, _ = backend
    _register(auth)
    auth.do(
        lambda service: service.register(
            tenant_id=OTHER_TENANT,
            login="s2400001",
            display_name="別機関の学生",
            password=PASSWORD,
        )
    )
    with pytest.raises(AuthenticationFailed):
        auth.do(
            lambda service: service.login(
                tenant_id=OTHER_TENANT, login="s2400001", password="wrong password"
            )
        )


def test_an_expired_session_stops_working(backend) -> None:
    auth, clock, _ = backend
    _register(auth)
    _, token = _login(auth)
    clock.advance(13)
    assert auth.do(lambda service: service.resolve(token)) is None


def test_logging_out_revokes_the_session(backend) -> None:
    auth, _, _ = backend
    _register(auth)
    _, token = _login(auth)
    auth.do(lambda service: service.logout(token))
    assert auth.do(lambda service: service.resolve(token)) is None


def test_changing_the_password_cuts_every_session(backend) -> None:
    auth, _, _ = backend
    principal = _register(auth)
    _, first = _login(auth)
    _, second = _login(auth)
    auth.do(
        lambda service: service.change_password(
            principal.user_id, current=PASSWORD, new="a new long password"
        )
    )
    assert auth.do(lambda service: service.resolve(first)) is None
    assert auth.do(lambda service: service.resolve(second)) is None


def test_disabling_keeps_the_user_row(backend) -> None:
    """消すと過去の提出と採点の参照が壊れる。"""
    auth, _, database = backend
    principal = _register(auth)
    auth.do(lambda service: service.disable(principal.user_id))

    from aijudge_persistence.schema import UserRow

    with database.session() as session:
        assert session.get(UserRow, str(principal.user_id)) is not None


def test_enrolment_decides_what_a_user_may_do(backend) -> None:
    auth, _, _ = backend
    learner = _register(auth, "learner")
    instructor = _register(auth, "instructor")
    course = Course(
        id=COURSE,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-前期",
        subject_profile="cs_intro_c",
    )
    auth.do(lambda service: service._repository.save_course(course))
    auth.do(
        lambda service: service.enroll(
            tenant_id=TENANT, course_id=COURSE, user_id=learner.user_id, role=Role.LEARNER
        )
    )
    auth.do(
        lambda service: service.enroll(
            tenant_id=TENANT,
            course_id=COURSE,
            user_id=instructor.user_id,
            role=Role.INSTRUCTOR,
        )
    )

    assert auth.do(lambda s: s.require_grader(COURSE, instructor.user_id)) is Role.INSTRUCTOR
    with pytest.raises(PermissionDenied):
        auth.do(lambda s: s.require_grader(COURSE, learner.user_id))
    assert [c.code for c in auth.do(lambda s: s.courses_for(TENANT, learner.user_id))] == ["prog2"]


def test_a_non_member_sees_no_courses(backend) -> None:
    auth, _, _ = backend
    principal = _register(auth)
    assert auth.do(lambda s: s.courses_for(TENANT, principal.user_id)) == ()
