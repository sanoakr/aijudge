"""デモコースへの自動受講登録（#194）。

**縛るのは「できること」より「できないこと」である。** 文字列の前方一致で
教員権限を配る仕掛けなので、範囲が広がる壊れ方がいちばん怖い。
"""

from __future__ import annotations

import pytest

from aijudge_audit import InMemoryAuditLog
from aijudge_core import Course, Enrollment, Role
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_identity import (
    DemoCourse,
    InMemoryIdentityRepository,
    Principal,
    demo_course_from_env,
    enrol_into_demo_course,
)

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)
DEMO = CourseId("crs_" + "d" * 32)
REAL = CourseId("crs_" + "1" * 32)


@pytest.fixture
def repository() -> InMemoryIdentityRepository:
    repo = InMemoryIdentityRepository()
    repo.save_course(
        Course(
            id=DEMO,
            tenant_id=TENANT,
            code="demo",
            title="お試しコース",
            term="2026-前期",
            subject_profile="cs_lang_c_intro",
        )
    )
    repo.save_course(
        Course(
            id=REAL,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング演習 II",
            term="2026-前期",
            subject_profile="cs_lang_c_intro",
        )
    )
    return repo


def _who(login: str, *, external: bool = True, admin: bool = False) -> Principal:
    return Principal(
        user_id=UserId("usr_" + login.ljust(32, "0")[:32]),
        tenant_id=TENANT,
        login=login,
        display_name=login,
        is_tenant_admin=admin,
        is_external=external,
    )


def _enrol(repository, principal, prefix: str = "a"):
    return enrol_into_demo_course(
        repository,
        principal,
        DemoCourse(course_id=DEMO, instructor_prefix=prefix),
        audit=InMemoryAuditLog(),
    )


# --------------------------------------------------------------------------
# 効くこと
# --------------------------------------------------------------------------


def test_anyone_who_logs_in_becomes_a_learner(repository) -> None:
    assert _enrol(repository, _who("y2399012")) is Role.LEARNER
    assert repository.find_enrollment(DEMO, _who("y2399012").user_id) is not None


def test_an_oauth_account_with_the_prefix_becomes_an_instructor(repository) -> None:
    """機関の教職員接頭辞（既定 `a`）で教員にする。"""
    assert _enrol(repository, _who("a12345")) is Role.INSTRUCTOR


def test_the_prefix_is_configurable(repository) -> None:
    """**機関のアカウント規約を写したものなので、焼き込まない。**

    他の機関では別の文字になる。
    """
    assert _enrol(repository, _who("t9999"), prefix="t") is Role.INSTRUCTOR
    assert _enrol(repository, _who("a12345"), prefix="t") is Role.LEARNER


def test_enrolling_twice_changes_nothing(repository) -> None:
    """**冪等。** ログインのたびに走る。"""
    assert _enrol(repository, _who("y2399012")) is Role.LEARNER
    assert _enrol(repository, _who("y2399012")) is None, "二度目に何かしている"


def test_an_existing_role_is_never_overwritten(repository) -> None:
    """**本物の教員が学生として入っている状態を書き換えない。**

    デモコースで学生の見え方を確かめている教員がいる。次のログインで
    勝手に教員へ戻すと、確かめたいものが確かめられない。
    """
    teacher = _who("a12345")
    repository.save_enrollment(
        Enrollment(tenant_id=TENANT, course_id=DEMO, user_id=teacher.user_id, role=Role.LEARNER)
    )

    assert _enrol(repository, teacher) is None
    enrollment = repository.find_enrollment(DEMO, teacher.user_id)
    assert enrollment is not None and enrollment.role is Role.LEARNER


# --------------------------------------------------------------------------
# 効かないこと ── **こちらが本題**
# --------------------------------------------------------------------------


def test_a_local_user_never_gets_the_prefix_rule(repository) -> None:
    """**接頭辞で教員になれるのは OAuth 利用者だけ。**

    ローカル利用者の `login` は管理者が自由に決められる（`aijudge-admin`）
    ので、そこに接頭辞の規則を効かせると「名前を選べば教員になれる」経路が
    できる。外部 IdP のアカウント名は機関が決めるので、そこには当てはまらない。
    """
    assert _enrol(repository, _who("admin", external=False)) is Role.LEARNER


def test_it_never_makes_anyone_a_tenant_admin(repository) -> None:
    """**テナント管理者には絶対に昇格させない。**

    デモコースの教員は、そのコースの中だけの役割である。
    """
    who = _who("a12345")
    _enrol(repository, who)
    user = repository.get_user(who.user_id)
    assert user is None or not user.is_tenant_admin


def test_it_touches_no_other_course(repository) -> None:
    """**他のコースの権限は 1 つも増えない。**"""
    who = _who("a12345")
    _enrol(repository, who)
    assert repository.find_enrollment(REAL, who.user_id) is None


def test_a_course_from_another_tenant_is_refused(repository) -> None:
    """指名を取り違えたときに、別テナントの利用者が入ってくる経路にしない。"""
    outsider = Principal(
        user_id=UserId("usr_" + "f" * 32),
        tenant_id=OTHER_TENANT,
        login="a12345",
        display_name="よそのテナント",
        is_external=True,
    )
    assert _enrol(repository, outsider) is None
    assert repository.find_enrollment(DEMO, outsider.user_id) is None


def test_a_missing_course_does_not_break_the_login(repository) -> None:
    """**古い ID が環境変数に残っている運用は普通に起きる。**

    ここで例外を投げると、ログインごと落ちる。
    """
    gone = DemoCourse(course_id=CourseId("crs_" + "0" * 32))
    assert (
        enrol_into_demo_course(repository, _who("y2399012"), gone, audit=InMemoryAuditLog()) is None
    )


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------


def test_the_feature_is_absent_unless_it_is_configured() -> None:
    """**未設定なら 1 行も動かない。**

    コース側のフラグにしなかったのはこのため ── 本番の DB を取り違えた
    ときに、本物のコースがデモになりうる。
    """
    assert demo_course_from_env({}) is None
    assert demo_course_from_env({"AIJUDGE_DEMO_COURSE": "  "}) is None


def test_an_empty_prefix_does_not_make_everyone_an_instructor() -> None:
    """空文字はすべての login に前方一致する。**設定ミスで全員が教員になる。**"""
    demo = demo_course_from_env(
        {"AIJUDGE_DEMO_COURSE": "crs_x", "AIJUDGE_DEMO_INSTRUCTOR_PREFIX": ""}
    )
    assert demo is not None and demo.instructor_prefix == "a"


# --------------------------------------------------------------------------
# ログインの経路に繋がっていること
# --------------------------------------------------------------------------


def test_logging_in_enrols_you(repository, monkeypatch) -> None:
    """**ログインの合流点で登録される。**

    パスワードと Google の 2 経路 × 2 アプリで呼び出し側は 4 か所あり、
    そこに書くと忘れる場所が 4 つできる。`_start_session` は 1 つしかない。
    """
    from aijudge_identity import AuthService

    monkeypatch.setenv("AIJUDGE_DEMO_COURSE", str(DEMO))
    service = AuthService(repository, audit=InMemoryAuditLog())
    principal = service.register(
        tenant_id=TENANT, login="y2400001", display_name="学生", password="correct horse battery"
    )
    assert repository.find_enrollment(DEMO, principal.user_id) is None, "登録しただけで入っている"

    service.login(tenant_id=TENANT, login="y2400001", password="correct horse battery")

    enrollment = repository.find_enrollment(DEMO, principal.user_id)
    assert enrollment is not None and enrollment.role is Role.LEARNER


def test_logging_in_without_the_setting_enrols_nobody(repository, monkeypatch) -> None:
    """**未設定なら何も起きない。**"""
    from aijudge_identity import AuthService

    monkeypatch.delenv("AIJUDGE_DEMO_COURSE", raising=False)
    service = AuthService(repository, audit=InMemoryAuditLog())
    principal = service.register(
        tenant_id=TENANT, login="y2400002", display_name="学生", password="correct horse battery"
    )
    service.login(tenant_id=TENANT, login="y2400002", password="correct horse battery")

    assert repository.find_enrollment(DEMO, principal.user_id) is None


def test_a_broken_demo_setting_does_not_block_the_login(repository, monkeypatch) -> None:
    """**ログインを落とさない。**

    誰でも試せる場所を用意する仕掛けが、入れなくなる理由になってはいけない。
    設定の取り違えもコースの消し忘れも、運用では普通に起きる。
    """
    from aijudge_identity import AuthService

    monkeypatch.setenv("AIJUDGE_DEMO_COURSE", "crs_" + "0" * 32)
    service = AuthService(repository, audit=InMemoryAuditLog())
    service.register(
        tenant_id=TENANT, login="y2400003", display_name="学生", password="correct horse battery"
    )

    _, token = service.login(tenant_id=TENANT, login="y2400003", password="correct horse battery")
    assert token, "デモコースの設定ミスでログインできなくなっている"
