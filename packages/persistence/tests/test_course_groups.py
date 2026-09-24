"""出題先の名簿は、インメモリ実装と SQL 実装で同じに振る舞う。

片方だけ通る規則は、移行した日に破綻する（`test_identity.py` と同じ理由）。
PostgreSQL は `AIJUDGE_TEST_DATABASE_URL` があるときだけ足す。

固定したいのは 5 つ。

置き換え       名簿は丸ごと置き換わる。2 度流しても同じ（API の冪等性の土台）。
逆引き         「この人はどのグループか」がコースで区切られる。
名前は一意     同じコースに同じ名前のグループを 2 つ作れない。別のコースなら作れる。
消す           グループを消すと名簿も消える。コースを消すとグループも消える。
幅             64 字の名前が PostgreSQL でも入る（SQLite は幅を守らない）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from aijudge_core import MAX_GROUP_NAME_LENGTH, Course, CourseGroup, new_id
from aijudge_core.ids import CourseGroupId, CourseId, TenantId, UserId
from aijudge_identity import InMemoryIdentityRepository
from aijudge_identity.models import User
from aijudge_persistence import Database

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
OTHER_COURSE = CourseId("crs_" + "2" * 32)
ALICE = UserId("usr_" + "a" * 32)
BOB = UserId("usr_" + "b" * 32)
CREATED = datetime(2026, 9, 24, tzinfo=UTC)


class Backend:
    """1 操作ごとに保存先を開いて閉じる。SQL では 1 操作 = 1 トランザクション。"""

    def __init__(self, run: Callable) -> None:
        self._run = run

    def __call__(self, action):
        return self._run(action)


@pytest.fixture(params=["memory", "sqlite"] + (["postgres"] if POSTGRES_URL else []))
def repo(request) -> Iterator[Backend]:
    if request.param == "memory":
        memory = InMemoryIdentityRepository()
        backend = Backend(lambda action: action(memory))
        _seed(backend)
        yield backend
        return

    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    database = Database.connect(url, create=False)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)

    @contextmanager
    def _uow():
        with database.unit_of_work() as uow:
            yield uow

    def run(action):
        with _uow() as uow:
            result = action(uow.identity)
            uow.commit()
        return result

    backend = Backend(run)
    _seed(backend)
    yield backend
    database.dispose()


def _seed(backend: Backend) -> None:
    def seed(identity) -> None:
        for course_id, code in ((COURSE, "prog2"), (OTHER_COURSE, "network")):
            identity.save_course(
                Course(
                    id=course_id,
                    tenant_id=TENANT,
                    code=code,
                    title=code,
                    term="2026-後期",
                    subject_profile="cs_lang_c_intro",
                )
            )
        for user_id, login in ((ALICE, "s2400001"), (BOB, "s2400002")):
            identity.save_user(
                User(
                    id=user_id,
                    tenant_id=TENANT,
                    login=login,
                    display_name=login,
                    # 名簿は認証を通らないので、ハッシュは形だけでよい。
                    password_hash="x",
                    created_at=CREATED,
                )
            )

    backend(seed)


def _group(name: str, course_id: CourseId = COURSE) -> CourseGroup:
    return CourseGroup(
        id=CourseGroupId(new_id("grp")), tenant_id=TENANT, course_id=course_id, name=name
    )


def test_members_are_replaced_as_a_whole(repo: Backend) -> None:
    retake = _group("追試")
    repo(lambda r: r.save_group(retake))

    repo(lambda r: r.set_group_members(retake.id, frozenset({ALICE, BOB})))
    repo(lambda r: r.set_group_members(retake.id, frozenset({BOB})))
    repo(lambda r: r.set_group_members(retake.id, frozenset({BOB})))

    assert repo(lambda r: r.group_members(retake.id)) == frozenset({BOB})


def test_groups_of_is_scoped_to_the_course(repo: Backend) -> None:
    here = _group("追試")
    there = _group("追試", OTHER_COURSE)
    for group in (here, there):
        repo(lambda r, g=group: r.save_group(g))
        repo(lambda r, g=group: r.set_group_members(g.id, frozenset({ALICE})))

    assert repo(lambda r: r.groups_of(COURSE, ALICE)) == frozenset({here.id})
    assert repo(lambda r: r.groups_of(COURSE, BOB)) == frozenset()


def test_a_name_is_unique_within_a_course(repo: Backend) -> None:
    repo(lambda r: r.save_group(_group("追試")))

    with pytest.raises((IntegrityError, ValueError)):
        repo(lambda r: r.save_group(_group("追試")))
    # 別のコースなら同じ名前を使える。
    repo(lambda r: r.save_group(_group("追試", OTHER_COURSE)))


def test_find_list_and_rename(repo: Backend) -> None:
    b = _group("2 組")
    a = _group("1 組")
    for group in (b, a):
        repo(lambda r, g=group: r.save_group(g))

    assert [g.name for g in repo(lambda r: r.list_groups(COURSE))] == ["1 組", "2 組"]
    assert repo(lambda r: r.find_group(COURSE, "1 組")) == a
    assert repo(lambda r: r.find_group(OTHER_COURSE, "1 組")) is None

    renamed = a.model_copy(update={"name": "1 組（月曜）"})
    repo(lambda r: r.save_group(renamed))
    assert repo(lambda r: r.get_group(a.id)) == renamed


def test_deleting_a_group_removes_its_members(repo: Backend) -> None:
    retake = _group("追試")
    repo(lambda r: r.save_group(retake))
    repo(lambda r: r.set_group_members(retake.id, frozenset({ALICE})))

    repo(lambda r: r.delete_group(retake.id))

    assert repo(lambda r: r.get_group(retake.id)) is None
    assert repo(lambda r: r.groups_of(COURSE, ALICE)) == frozenset()


def test_deleting_the_course_removes_its_groups(repo: Backend) -> None:
    retake = _group("追試")
    repo(lambda r: r.save_group(retake))
    repo(lambda r: r.set_group_members(retake.id, frozenset({ALICE})))

    repo(lambda r: r.delete_course(COURSE))

    assert repo(lambda r: r.list_groups(COURSE)) == ()
    assert repo(lambda r: r.groups_of(COURSE, ALICE)) == frozenset()


def test_the_longest_name_fits_the_column(repo: Backend) -> None:
    """**模型の上限と列の幅が揃っていること。** SQLite では確かめられない。"""
    longest = _group("名" * MAX_GROUP_NAME_LENGTH)
    repo(lambda r: r.save_group(longest))

    assert repo(lambda r: r.get_group(longest.id)) == longest


def test_a_name_longer_than_the_column_is_refused_by_the_model() -> None:
    with pytest.raises(ValueError):
        _group("名" * (MAX_GROUP_NAME_LENGTH + 1))
