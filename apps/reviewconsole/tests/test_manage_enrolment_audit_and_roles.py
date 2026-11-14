"""受講者の画面の操作を監査に残し、名簿の貼り付けが役割の保護を通り抜けないようにする（#700）。

画面で受講者の役割を変える・名簿で受講登録する・受講を取り消す操作は、権限の変更である
（ADR 0016「権限変更の痕跡が残らない」が動機）。以前は 3 つとも監査に 1 行も残らなかった。

もう 1 つ、`set_role` は自分の役割の変更と管理者の役割の変更を断るのに、名簿の貼り付けと
受講の取り消しにはその保護が無かった。学期の名簿（自分や同僚の login を含む一覧）を既定の
役割で貼り直すと、自分と同僚の教員がそろって学習者に落ち、コースの管理画面に入れなくなる。

固定したいのは 4 つ。

残す        役割の変更・名簿の登録・受講の取り消しが、同じ作業単位で監査に 1 行ずつ残る
動かさない   名簿は、自分の役割と管理者の役割を変えない（変えなかったことを画面に言う）
断る        管理者の受講は、画面から取り消せない
変わらない   上の保護に当たらない行は、今までどおり登録・更新される
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_manage import TENANT, World

from aijudge_audit import AuditAction
from aijudge_core import Role
from aijudge_core.ids import TenantId


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


def _audit_rows(world: World):
    with world.database.unit_of_work() as uow:
        return uow.audit.list_recent(TenantId(TENANT), action=AuditAction.ENROLLED)


def _role_of(world: World, user_id) -> Role | None:
    with world.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(world.course.id, user_id)
    return None if enrollment is None else enrollment.role


def test_changing_a_role_is_recorded_with_both_values(world: World) -> None:
    teacher = world.register("teacher", Role.INSTRUCTOR)
    student = world.register("s2400001", Role.LEARNER)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/role",
        data={"role": "assistant"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    (row,) = _audit_rows(world)
    assert row.actor_user_id == teacher.user_id
    assert row.target_id == str(student.user_id)
    assert row.detail["role"] == {"before": "learner", "after": "assistant"}
    assert row.detail["course_id"] == str(world.course.id)
    assert row.request_id is not None


def test_a_pasted_roster_is_recorded_once_with_the_counts(world: World) -> None:
    teacher = world.register("teacher", Role.INSTRUCTOR)
    world.register("y239999", None)
    world.register("y239998", Role.LEARNER)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "y239999\ny239998", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    (row,) = _audit_rows(world)
    assert row.actor_user_id == teacher.user_id
    assert row.detail["enrolled"] == ["y239999"]
    assert row.detail["already"] == ["y239998"]
    assert row.detail["course_id"] == str(world.course.id)


def test_removing_an_enrolment_is_recorded(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    student = world.register("s2400001", Role.LEARNER)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{student.user_id}/remove",
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    assert _role_of(world, student.user_id) is None
    (row,) = _audit_rows(world)
    assert row.target_id == str(student.user_id)
    assert row.detail["role"] == {"before": "learner", "after": None}


def test_a_roster_does_not_demote_an_administrator(world: World) -> None:
    """既存の管理者の行を既定の役割で貼っても、役割は変わらない（`set_role` と同じ保護）。"""
    world.register("teacher", Role.INSTRUCTOR)
    chief = world.register("chief", Role.ADMIN)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "chief", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    assert _role_of(world, chief.user_id) is Role.ADMIN, "名簿で管理者が学習者に落ちた"
    # 変えなかったことを言う。黙って飛ばすと、教員は入ったものと読む。
    assert "protected=chief" in response.headers["location"]
    page = world.client("teacher").get(response.headers["location"]).text
    assert "変更しませんでした" in page


def test_a_roster_does_not_change_the_callers_own_role(world: World) -> None:
    """自分の行を貼っても、自分の役割は変わらない（変わると管理画面に入れなくなる）。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    response = client.post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "teacher", "role": "learner"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    assert _role_of(world, teacher.user_id) is Role.INSTRUCTOR
    assert client.get(f"/manage/courses/{world.course.id}/enrolments").status_code == 200


def test_the_rest_of_the_roster_is_still_enrolled_around_a_protected_row(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    chief = world.register("chief", Role.ADMIN)
    newcomer = world.register("y239999", None)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "chief\ny239999", "role": "learner"},
        follow_redirects=False,
    )

    assert _role_of(world, chief.user_id) is Role.ADMIN
    assert _role_of(world, newcomer.user_id) is Role.LEARNER


def test_a_roster_still_promotes_an_ordinary_learner(world: World) -> None:
    """保護に当たらない既存の受講者の役割は、今までどおり名簿で変えられる。"""
    world.register("teacher", Role.INSTRUCTOR)
    student = world.register("s2400001", Role.LEARNER)

    world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments",
        data={"roster": "s2400001", "role": "assistant"},
        follow_redirects=False,
    )

    assert _role_of(world, student.user_id) is Role.ASSISTANT


def test_an_administrators_enrolment_cannot_be_removed_from_the_screen(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    chief = world.register("chief", Role.ADMIN)

    response = world.client("teacher").post(
        f"/manage/courses/{world.course.id}/enrolments/{chief.user_id}/remove",
        follow_redirects=False,
    )

    assert response.status_code == 403, response.text
    assert _role_of(world, chief.user_id) is Role.ADMIN
    assert _audit_rows(world) == ()
