"""「いま使っている人数」（2026-10-08）。

ログインの有効期限（12 時間）とは別に、**最後に操作した時刻**を持ち、直近 5 分に
操作があった利用者を数える。メモリ実装と DB 実装に同じテストを当てる。固定したいのは 7 つ。

ログインしただけは数えない   操作の記録が無い（スリープ・閉じた画面と同じ）
操作すれば数える             `resolve` が最終操作を記録する
窓を過ぎたら数えない         5 分より前の操作は数えない
自動更新は数えない           `touch=False` の取得は記録しない
書くのは間引く               60 秒未満の再取得では書き直さない
同じ人は 1 人                複数のセッションでも人数は 1
切れたセッションは数えない   ログアウト・期限切れ
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_audit import InMemoryAuditLog
from aijudge_core import Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import ActiveUsers, AuthService, InMemoryIdentityRepository
from aijudge_identity.service import ACTIVE_WINDOW, TOUCH_INTERVAL, _token_hash
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class World:
    def __init__(self, service: AuthService, repository, clock: Clock, commit) -> None:
        self.service = service
        self.repository = repository
        self.clock = clock
        self._commit = commit

    def login(self, login: str) -> str:
        with contextlib.suppress(Exception):  # 登録済みなら登録しない
            self.service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
        _, token = self.service.login(tenant_id=TENANT, login=login, password=PASSWORD)
        return token

    def active(self) -> ActiveUsers:
        return self.service.active_users(TENANT)

    def last_seen(self, token: str):
        session = self.repository.find_session_by_token_hash(_token_hash(token))
        return None if session is None else session.last_seen_at


@pytest.fixture(params=["memory", "sql"])
def world(request, tmp_path) -> Iterator[World]:
    clock = Clock()
    if request.param == "memory":
        repository = InMemoryIdentityRepository()
        yield World(
            AuthService(repository, audit=InMemoryAuditLog(), clock=clock),
            repository,
            clock,
            lambda: None,
        )
        return
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    uow_cm = database.unit_of_work()
    uow = uow_cm.__enter__()
    try:
        yield World(
            AuthService(uow.identity, audit=uow.audit, clock=clock), uow.identity, clock, uow.commit
        )
    finally:
        uow_cm.__exit__(None, None, None)
        database.dispose()


def test_logging_in_alone_does_not_count(world: World) -> None:
    world.login("s1")
    assert world.active().total == 0, "ログインしただけで使用中に数えている"


def test_an_operation_counts_and_the_window_closes(world: World) -> None:
    token = world.login("s1")
    world.service.resolve(token)
    assert world.active().total == 1

    world.clock.advance(seconds=ACTIVE_WINDOW.total_seconds() - 1)
    assert world.active().total == 1, "窓の内側なのに数えない"
    world.clock.advance(seconds=2)
    assert world.active().total == 0, "5 分前の操作を数えている"

    world.service.resolve(token)
    assert world.active().total == 1, "もう一度操作したら数える"


def test_a_background_refresh_does_not_count(world: World) -> None:
    token = world.login("s1")
    world.service.resolve(token, touch=False)
    assert world.last_seen(token) is None
    assert world.active().total == 0


def test_the_record_is_not_rewritten_within_the_interval(world: World) -> None:
    token = world.login("s1")
    world.service.resolve(token)
    first = world.last_seen(token)

    world.clock.advance(seconds=TOUCH_INTERVAL.total_seconds() - 1)
    world.service.resolve(token)
    assert world.last_seen(token) == first, "間引かずに書き直している"

    world.clock.advance(seconds=2)
    world.service.resolve(token)
    assert world.last_seen(token) > first, "間隔を過ぎても書き直さない"


def test_one_person_with_two_sessions_is_one_user(world: World) -> None:
    for token in (world.login("s1"), world.login("s1")):
        world.service.resolve(token)
    assert world.active().total == 1


def test_a_logged_out_or_expired_session_does_not_count(world: World) -> None:
    gone = world.login("s1")
    world.service.resolve(gone)
    world.service.logout(gone)
    assert world.active().total == 0, "ログアウトしたのに数えている"

    stale = world.login("s2")
    world.service.resolve(stale)
    world.clock.advance(hours=13)
    assert world.active().total == 0, "期限切れのセッションを数えている"


def test_staff_are_counted_separately_from_learners(world: World) -> None:
    learner = world.login("learner")
    teacher = world.login("teacher")
    admin = world.login("admin")
    # 受講の登録は主体から行う
    principals = {
        login: world.service.resolve(token, touch=False)
        for login, token in (("learner", learner), ("teacher", teacher), ("admin", admin))
    }
    world.service.enroll(
        tenant_id=TENANT, course_id=COURSE, user_id=principals["learner"].user_id, role=Role.LEARNER
    )
    world.service.enroll(
        tenant_id=TENANT,
        course_id=COURSE,
        user_id=principals["teacher"].user_id,
        role=Role.INSTRUCTOR,
    )
    world.service.set_tenant_admin(principals["admin"].user_id, admin=True)
    world._commit()
    for token in (learner, teacher, admin):
        world.service.resolve(token)

    counted = world.active()

    assert (counted.total, counted.staff, counted.learners) == (3, 2, 1)
