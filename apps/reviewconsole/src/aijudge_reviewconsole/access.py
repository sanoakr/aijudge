"""「このコースの担当教員か」を HTTP の応答に読み替える（段階的な立て直し 1-1）。

判定そのもの（役割が教員か）は `AuthService.require_instructor` にある。以前は
同じ判定がコンソールの 4 か所に別々に書かれていた ── `manage._require_instructor`・
`api._require_instructor`（一字一句同じ）、`app._require_course_instructor`（文言だけ
違う）、`app._is_course_instructor`（拒まずに真偽を返す）。1 つを直しても他が
残る形だった。

**応答の規則はここ 1 か所**:

- 一員でない → **404**（存在と権限を区別しない。コースを列挙させない）
- 一員だが教員でない（TA・学習者）→ **403**（何が足りないかを言う）

文言は画面ごとに違ってよい（確定の経路は「提出が見つかりません」と言う）ので、
引数で受ける。振る舞いは移す前と同じ。
"""

from __future__ import annotations

from fastapi import HTTPException

from aijudge_core import Course
from aijudge_core.ids import CourseId
from aijudge_identity import AuthService, NotAnInstructor, PermissionDenied, Principal

#: 一員でないときの既定の文言。
COURSE_NOT_FOUND = "コースが見つかりません"
#: 一員だが教員でないときの既定の文言。
INSTRUCTOR_REQUIRED = "この操作には担当教員の権限が必要です"


def require_instructor(
    console,
    me: Principal,
    course_id: CourseId,
    *,
    not_found: str = COURSE_NOT_FOUND,
    forbidden: str = INSTRUCTOR_REQUIRED,
) -> Course:
    """このコースの担当教員であること。**TA には開けない。** コースを返す。"""
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            auth.require_instructor(course_id, me.user_id)
        except NotAnInstructor as exc:
            raise HTTPException(status_code=403, detail=forbidden) from exc
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail=not_found) from exc
        course = uow.identity.get_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail=not_found)
    return course


def is_instructor(console, me: Principal, course_id: CourseId) -> bool:
    """このコースの担当教員か。**拒むのではなく、出し分けに使う**（#275）。"""
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            auth.require_instructor(course_id, me.user_id)
        except PermissionDenied:
            return False
    return True


__all__ = [
    "COURSE_NOT_FOUND",
    "INSTRUCTOR_REQUIRED",
    "is_instructor",
    "require_instructor",
]
