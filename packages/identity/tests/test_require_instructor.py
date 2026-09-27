"""「このコースの担当教員か」の規則（段階的な立て直し 1-1）。

以前はコンソールの 4 か所に別々に書かれていた判定を `AuthService` に置いた。
固定したいのは、一員でない人と、一員だが教員でない人を**分けて**断ること ──
画面は前者に 404、後者に 403 で答える。
"""

from __future__ import annotations

import pytest

from aijudge_audit import InMemoryAuditLog
from aijudge_core import Course, Role
from aijudge_core.ids import CourseId, TenantId
from aijudge_identity import (
    INSTRUCTOR_ROLES,
    AuthService,
    InMemoryIdentityRepository,
    NotAnInstructor,
    PermissionDenied,
)

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
PASSWORD = "correct horse battery"


@pytest.fixture
def service() -> AuthService:
    repository = InMemoryIdentityRepository()
    repository.save_course(
        Course(
            id=COURSE,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング",
            term="2026-後期",
            subject_profile="cs_lang_c_intro",
        )
    )
    return AuthService(repository, audit=InMemoryAuditLog())


def _member(service: AuthService, login: str, role: Role | None):
    principal = service.register(
        tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
    )
    if role is not None:
        service.enroll(tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=role)
    return principal


@pytest.mark.parametrize("role", [Role.INSTRUCTOR, Role.ADMIN])
def test_an_instructor_passes(service: AuthService, role: Role) -> None:
    me = _member(service, "teacher", role)
    assert service.require_instructor(COURSE, me.user_id) is role


@pytest.mark.parametrize("role", [Role.ASSISTANT, Role.LEARNER])
def test_a_member_who_is_not_an_instructor_is_told_so(service: AuthService, role: Role) -> None:
    me = _member(service, "someone", role)
    with pytest.raises(NotAnInstructor):
        service.require_instructor(COURSE, me.user_id)


def test_a_non_member_is_refused_without_saying_why(service: AuthService) -> None:
    """一員でない人には「教員でない」と言わない（コースの存在を明かさない）。"""
    me = _member(service, "stranger", None)
    with pytest.raises(PermissionDenied) as caught:
        service.require_instructor(COURSE, me.user_id)
    assert not isinstance(caught.value, NotAnInstructor)


def test_a_tenant_admin_passes_without_an_enrolment(service: AuthService) -> None:
    """テナント管理者は受講登録なしで `ADMIN`（#128）。"""
    me = _member(service, "admin", None)
    service.set_tenant_admin(me.user_id, admin=True)
    assert service.require_instructor(COURSE, me.user_id) is Role.ADMIN


def test_the_roles_are_the_ones_that_may_edit() -> None:
    assert frozenset({Role.INSTRUCTOR, Role.ADMIN}) == INSTRUCTOR_ROLES
