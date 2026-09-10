"""デモコースへの自動受講登録（#194）。

触って試せるコースを 1 つ置く。ログインすれば誰でも入れ、提出も採点も動くが、
**学習履歴にも評価にも一切残らない**（数えない側は `is_trial` が担う・#197）。

ここが持つのは受講登録の規則だけである。

**未設定なら機能ごと無い。** デモコースは環境変数で指名する ── コース側の
フラグにすると、本番の DB を取り違えたときに本物のコースがデモになりうる。
「設定した人だけが持つ機能」にしておけば、設定しなければ 1 行も動かない。

**教員に上げる規則は、この 1 コースの中だけで効く。** 文字列の前方一致で
権限を配る仕掛けなので、範囲を広げてはいけない ── テナント管理者には
昇格させず、他のコースの権限は 1 つも増やさない。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from aijudge_audit import AuditAction, AuditLog, AuditRecorder
from aijudge_core import Enrollment, Role
from aijudge_core.ids import CourseId

from .models import Principal
from .repository import IdentityRepository

__all__ = ["DemoCourse", "demo_course_from_env", "enrol_into_demo_course"]

logger = logging.getLogger(__name__)

#: デモコースの ID。**未設定ならこの機能は無効。**
ENV_DEMO_COURSE = "AIJUDGE_DEMO_COURSE"
#: 教員として登録する OAuth アカウントの接頭辞。
#:
#: **機関のアカウント規約を写したものなので、コードに焼き込まない**（既定は
#: 龍谷大学の教職員接頭辞）。他機関では別の文字になる。
ENV_DEMO_INSTRUCTOR_PREFIX = "AIJUDGE_DEMO_INSTRUCTOR_PREFIX"
DEFAULT_INSTRUCTOR_PREFIX = "a"


@dataclass(frozen=True)
class DemoCourse:
    """デモコースの設定。"""

    course_id: CourseId
    instructor_prefix: str = DEFAULT_INSTRUCTOR_PREFIX


def demo_course_from_env(env: dict[str, str] | None = None) -> DemoCourse | None:
    """環境からデモコースの設定を読む。**未設定なら `None`。**"""
    source = os.environ if env is None else env
    course_id = (source.get(ENV_DEMO_COURSE) or "").strip()
    if not course_id:
        return None
    prefix = (source.get(ENV_DEMO_INSTRUCTOR_PREFIX) or DEFAULT_INSTRUCTOR_PREFIX).strip()
    if not prefix:
        # 空の接頭辞は**全員に一致する**。教員権限を全員に配ることになるので、
        # 設定ミスとして既定に戻す（黙って全員を教員にしない）。
        logger.warning("%s is empty; falling back to the default", ENV_DEMO_INSTRUCTOR_PREFIX)
        prefix = DEFAULT_INSTRUCTOR_PREFIX
    return DemoCourse(course_id=CourseId(course_id), instructor_prefix=prefix)


def _role_for(principal: Principal, demo: DemoCourse) -> Role:
    """この人をデモコースにどの役割で入れるか。

    **教員に上げるのは OAuth 利用者だけ。** ローカルのパスワード利用者は
    `login` を管理者が自由に決められる（`aijudge-admin`）ので、接頭辞で
    権限を配ると「名前を選べば教員になれる」経路ができる。外部 IdP の
    アカウント名は機関が決めるので、そこには当てはまらない。
    """
    if principal.is_external and principal.login.startswith(demo.instructor_prefix):
        return Role.INSTRUCTOR
    return Role.LEARNER


def enrol_into_demo_course(
    repository: IdentityRepository,
    principal: Principal,
    demo: DemoCourse,
    *,
    audit: AuditLog,
    request_id: str | None = None,
) -> Role | None:
    """デモコースに受講登録する。登録したときだけその役割を返す。

    **冪等。** ログインのたびに走るので、既に受講登録があれば何もしない ──
    **役割の上書きもしない。** 本物の教員が学生として入っている状態を、
    次のログインで書き換えてはいけない。

    **コースが無ければ何もしない。** 環境変数に古い ID が残っている運用は
    普通に起きるので、そこで例外を投げるとログインごと落ちる。
    """
    course = repository.get_course(demo.course_id)
    if course is None:
        logger.warning("demo course %s does not exist; skipping enrolment", demo.course_id)
        return None
    if course.tenant_id != principal.tenant_id:
        # テナントをまたいで登録しない。**指名を取り違えたときに、別テナント
        # の利用者が入ってくる経路にしない。**
        logger.warning("demo course %s belongs to another tenant; skipping", demo.course_id)
        return None
    if repository.find_enrollment(demo.course_id, principal.user_id) is not None:
        return None

    role = _role_for(principal, demo)
    repository.save_enrollment(
        Enrollment(
            tenant_id=principal.tenant_id,
            course_id=demo.course_id,
            user_id=principal.user_id,
            role=role,
        )
    )
    # **`system` として残す**（ADR 0016）── 人が押した操作ではない。
    # 利用者に帰属させると、その人が自分を教員にしたように読める。
    AuditRecorder.for_system(audit, tenant_id=principal.tenant_id, request_id=request_id).record(
        AuditAction.ENROLLED,
        target_type="enrolment",
        target_id=f"{demo.course_id}:{principal.user_id}",
        summary=f"デモコースに自動登録した（{role.value}）",
        detail={"course_id": str(demo.course_id), "role": role.value, "reason": "demo"},
    )
    return role
