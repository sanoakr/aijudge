"""出題先の名簿を操作する規則（`aijudge_admin.groups`）。

画面・API・CLI はすべてここを通るので、規則はここで固定する。

一部だけ登録しない   知らない login が 1 つでもあれば、何も保存しない。
学習者だけ           TA・教員・他コースの学習者は名簿に入れられない。
冪等                 同じ名簿を 2 度流すと、2 度目は何も変わらない（足し引きを返す）。
記録                 名簿の変更も、出題先の変更も、監査ログに残る。
消せない             出題先として使われているグループは消せない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin import ensure_course
from aijudge_admin.groups import (
    GroupInUse,
    GroupNotFound,
    UnknownLogins,
    delete_group,
    list_groups,
    replace_group_members,
    set_audience,
)
from aijudge_audit import AuditRecorder
from aijudge_core import Role, Task, new_id
from aijudge_core.ids import TaskId, TenantId, UserId
from aijudge_identity import AuthService
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習 II",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    with database.unit_of_work() as uow:
        service = AuthService(uow.identity, audit=uow.audit)
        for login, role in (
            ("s2400001", Role.LEARNER),
            ("s2400002", Role.LEARNER),
            ("s2400003", Role.LEARNER),
            ("ta", Role.ASSISTANT),
        ):
            user = service.register(
                tenant_id=TENANT, login=login, display_name=login, password="p" * 12
            )
            service.enroll(tenant_id=TENANT, course_id=course.id, user_id=user.user_id, role=role)
        # 受講していない学生（別のコースの学生を想定）。
        service.register(tenant_id=TENANT, login="s2499999", display_name="x", password="p" * 12)
        for key in ("r1", "r2"):
            uow.tasks.save_task(
                Task(id=TaskId(new_id("tsk")), course_id=course.id, title=key, unit="retake")
            )
        uow.commit()
    yield database, course
    database.dispose()


def _do(database, action):
    with database.unit_of_work() as uow:
        recorder = AuditRecorder.for_user(uow.audit, tenant_id=TENANT, user_id=TEACHER)
        result = action(uow, recorder)
        uow.commit()
    return result


def _replace(database, course, logins, name="追試"):
    return _do(
        database,
        lambda uow, rec: replace_group_members(uow, rec, course=course, name=name, logins=logins),
    )


def _actions(database) -> list[str]:
    with database.unit_of_work() as uow:
        return [e.action.value for e in uow.audit.list_recent(TENANT, limit=50)]


def test_creating_and_replacing_reports_what_changed(world) -> None:
    database, course = world

    first = _replace(database, course, ["s2400001", "s2400002", " s2400001 ", ""])
    assert first.created
    assert first.added == ("s2400001", "s2400002")

    second = _replace(database, course, ["s2400002", "s2400003"])
    assert not second.created
    assert second.added == ("s2400003",)
    assert second.removed == ("s2400001",)
    assert second.members == ("s2400002", "s2400003")


def test_the_same_roster_twice_changes_nothing(world) -> None:
    database, course = world
    _replace(database, course, ["s2400001"])

    again = _replace(database, course, ["s2400001"])

    assert (again.added, again.removed) == ((), ())


@pytest.mark.parametrize(
    "stranger",
    [
        "nobody",  # 存在しない
        "s2499999",  # 利用者はいるがこのコースを受講していない
        "ta",  # 受講しているが学習者ではない
    ],
)
def test_one_unknown_login_saves_nothing(world, stranger: str) -> None:
    """**一部だけ登録しない。** 漏れた学生は追試が見えないまま試験を迎える。"""
    database, course = world

    with pytest.raises(UnknownLogins) as caught:
        _replace(database, course, ["s2400001", stranger])

    assert caught.value.logins == (stranger,)
    with database.unit_of_work() as uow:
        assert uow.identity.find_group(course.id, "追試") is None


def test_changes_are_audited(world) -> None:
    database, course = world
    _replace(database, course, ["s2400001"])
    tasks = _tasks(database, course)
    _do(
        database,
        lambda uow, rec: set_audience(
            uow, rec, course=course, tasks=tasks, names=["追試"], unit_label="retake"
        ),
    )

    actions = _actions(database)
    assert "group.updated" in actions
    assert "task.updated" in actions


def test_the_audience_is_set_on_every_task_of_the_set_and_cleared_by_nothing(world) -> None:
    database, course = world
    summary = _replace(database, course, ["s2400001"])
    tasks = _tasks(database, course)

    def audience(names):
        return _do(
            database,
            lambda uow, rec: set_audience(
                uow, rec, course=course, tasks=_tasks_in(uow, course), names=names, unit_label="r"
            ),
        )

    saved = audience(["追試"])
    assert {task.audience_group_ids for task in saved} == {(summary.group.id,)}
    assert len(saved) == len(tasks)

    cleared = audience([])
    assert {task.audience_group_ids for task in cleared} == {()}


def test_an_unknown_group_name_is_refused(world) -> None:
    database, course = world
    tasks = _tasks(database, course)

    with pytest.raises(GroupNotFound):
        _do(
            database,
            lambda uow, rec: set_audience(
                uow, rec, course=course, tasks=tasks, names=["無い"], unit_label="r"
            ),
        )


def test_a_group_in_use_cannot_be_deleted(world) -> None:
    """消すと、その課題の出題先が存在しない名簿になり、誰にも見えなくなる。"""
    database, course = world
    _replace(database, course, ["s2400001"])
    tasks = _tasks(database, course)
    _do(
        database,
        lambda uow, rec: set_audience(
            uow, rec, course=course, tasks=tasks, names=["追試"], unit_label="r"
        ),
    )

    with pytest.raises(GroupInUse):
        _do(database, lambda uow, rec: delete_group(uow, rec, course=course, name="追試"))


def test_an_unused_group_can_be_deleted(world) -> None:
    database, course = world
    _replace(database, course, ["s2400001"])

    _do(database, lambda uow, rec: delete_group(uow, rec, course=course, name="追試"))

    with database.unit_of_work() as uow:
        assert list_groups(uow, course) == ()
    assert "group.deleted" in _actions(database)


def _tasks(database, course):
    with database.unit_of_work() as uow:
        return _tasks_in(uow, course)


def _tasks_in(uow, course):
    return tuple(uow.tasks.list_for_course(course.id))
