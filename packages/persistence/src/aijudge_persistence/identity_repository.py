"""S1 のプロトコルの SQLAlchemy 実装。

インメモリ実装と同じ規則を守る。特に:

- ログイン ID はテナント内で一意（DB の一意制約が最後の砦）
- セッションはトークンのハッシュで引く
- 利用者を**削除しない**（無効化するだけ。過去の提出と採点が参照している）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from aijudge_core import Course, Enrollment, Role
from aijudge_core.ids import ApiTokenId, CourseId, SessionId, TenantId, UserId
from aijudge_identity.models import ApiToken, Session, User, UserState

from .schema import ApiTokenRow, CourseRow, EnrollmentRow, SessionRow, UserRow


class SqlIdentityRepository:
    def __init__(self, session: DbSession) -> None:
        self._session = session

    # -- 利用者 ------------------------------------------------------------

    def save_user(self, user: User) -> None:
        row = self._session.get(UserRow, str(user.id))
        if row is None:
            self._session.add(
                UserRow(
                    id=str(user.id),
                    tenant_id=str(user.tenant_id),
                    login=user.login,
                    display_name=user.display_name,
                    email=user.email,
                    password_hash=user.password_hash,
                    state=user.state.value,
                    created_at=user.created_at,
                )
            )
        else:
            row.display_name = user.display_name
            row.email = user.email
            row.password_hash = user.password_hash
            row.state = user.state.value
        self._session.flush()

    def get_user(self, user_id: UserId) -> User | None:
        return _user(self._session.get(UserRow, str(user_id)))

    def find_user_by_login(self, tenant_id: TenantId, login: str) -> User | None:
        row = (
            self._session.execute(
                select(UserRow).where(UserRow.tenant_id == str(tenant_id), UserRow.login == login)
            )
            .scalars()
            .first()
        )
        return _user(row)

    # -- セッション --------------------------------------------------------

    def save_session(self, session: Session) -> None:
        self._session.add(
            SessionRow(
                id=str(session.id),
                user_id=str(session.user_id),
                tenant_id=str(session.tenant_id),
                token_hash=session.token_hash,
                created_at=session.created_at,
                expires_at=session.expires_at,
                revoked_at=session.revoked_at,
            )
        )
        self._session.flush()

    def find_session_by_token_hash(self, token_hash: str) -> Session | None:
        row = (
            self._session.execute(select(SessionRow).where(SessionRow.token_hash == token_hash))
            .scalars()
            .first()
        )
        if row is None:
            return None
        return Session(
            id=SessionId(row.id),
            user_id=UserId(row.user_id),
            tenant_id=TenantId(row.tenant_id),
            token_hash=row.token_hash,
            created_at=row.created_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    def revoke_session(self, session_id: str, at: datetime) -> None:
        self._session.execute(
            update(SessionRow)
            .where(SessionRow.id == str(session_id), SessionRow.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        self._session.flush()

    def revoke_sessions_for(self, user_id: UserId, at: datetime) -> None:
        self._session.execute(
            update(SessionRow)
            .where(SessionRow.user_id == str(user_id), SessionRow.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        self._session.flush()

    # -- API トークン ------------------------------------------------------

    def save_api_token(self, token: ApiToken) -> None:
        self._session.add(
            ApiTokenRow(
                id=str(token.id),
                tenant_id=str(token.tenant_id),
                user_id=str(token.user_id),
                token_hash=token.token_hash,
                note=token.note,
                created_at=token.created_at,
                expires_at=token.expires_at,
                revoked_at=token.revoked_at,
                last_used_at=token.last_used_at,
            )
        )
        self._session.flush()

    def find_api_token_by_hash(self, token_hash: str) -> ApiToken | None:
        row = (
            self._session.execute(
                select(ApiTokenRow).where(ApiTokenRow.token_hash == token_hash)
            )
            .scalars()
            .first()
        )
        return _api_token(row)

    def touch_api_token(self, token_id: ApiTokenId, at: datetime) -> None:
        row = self._session.get(ApiTokenRow, str(token_id))
        if row is not None:
            row.last_used_at = at
            self._session.flush()

    def revoke_api_token(self, token_id: ApiTokenId, at: datetime) -> None:
        row = self._session.get(ApiTokenRow, str(token_id))
        if row is not None and row.revoked_at is None:
            row.revoked_at = at
            self._session.flush()

    def list_api_tokens(self, tenant_id: TenantId) -> tuple[ApiToken, ...]:
        rows = self._session.execute(
            select(ApiTokenRow)
            .where(ApiTokenRow.tenant_id == str(tenant_id))
            .order_by(ApiTokenRow.created_at)
        ).scalars()
        return tuple(token for token in (_api_token(row) for row in rows) if token is not None)

    # -- コースと受講 ------------------------------------------------------

    def save_course(self, course: Course) -> None:
        row = self._session.get(CourseRow, str(course.id))
        if row is None:
            self._session.add(
                CourseRow(
                    id=str(course.id),
                    tenant_id=str(course.tenant_id),
                    code=course.code,
                    title=course.title,
                    term=course.term,
                    subject_profile=course.subject_profile,
                    auto_finalize_after_hours=course.auto_finalize_after_hours,
                )
            )
        else:
            row.title = course.title
            row.subject_profile = course.subject_profile
            row.auto_finalize_after_hours = course.auto_finalize_after_hours
        self._session.flush()

    def get_course(self, course_id: CourseId) -> Course | None:
        return _course(self._session.get(CourseRow, str(course_id)))

    def save_enrollment(self, enrollment: Enrollment) -> None:
        row = self._session.get(EnrollmentRow, (str(enrollment.course_id), str(enrollment.user_id)))
        if row is None:
            self._session.add(
                EnrollmentRow(
                    course_id=str(enrollment.course_id),
                    user_id=str(enrollment.user_id),
                    tenant_id=str(enrollment.tenant_id),
                    role=enrollment.role.value,
                )
            )
        else:
            row.role = enrollment.role.value
        self._session.flush()

    def find_enrollment(self, course_id: CourseId, user_id: UserId) -> Enrollment | None:
        row = self._session.get(EnrollmentRow, (str(course_id), str(user_id)))
        if row is None:
            return None
        return Enrollment(
            tenant_id=TenantId(row.tenant_id),
            course_id=CourseId(row.course_id),
            user_id=UserId(row.user_id),
            role=Role(row.role),
        )

    def list_courses_for_user(self, tenant_id: TenantId, user_id: UserId) -> tuple[Course, ...]:
        rows = self._session.execute(
            select(CourseRow)
            .join(EnrollmentRow, EnrollmentRow.course_id == CourseRow.id)
            .where(
                EnrollmentRow.tenant_id == str(tenant_id),
                EnrollmentRow.user_id == str(user_id),
            )
            .order_by(CourseRow.term, CourseRow.code)
        ).scalars()
        return tuple(_course(row) for row in rows if row is not None)  # type: ignore[misc]

    def remove_enrollment(self, course_id: CourseId, user_id: UserId) -> None:
        """受講を取り消す。**利用者の行は残す。**

        過去の提出と採点が利用者を参照しているので、消すと成績の履歴が壊れる。
        """
        row = self._session.get(EnrollmentRow, (str(course_id), str(user_id)))
        if row is not None:
            self._session.delete(row)
            self._session.flush()

    def list_users(self, tenant_id: TenantId, logins: tuple[str, ...]) -> tuple[User, ...]:
        if not logins:
            return ()
        rows = self._session.execute(
            select(UserRow).where(
                UserRow.tenant_id == str(tenant_id), UserRow.login.in_(list(logins))
            )
        ).scalars()
        return tuple(user for row in rows if (user := _user(row)) is not None)

    def list_enrollments(self, course_id: CourseId) -> tuple[Enrollment, ...]:
        rows = self._session.execute(
            select(EnrollmentRow)
            .where(EnrollmentRow.course_id == str(course_id))
            .order_by(EnrollmentRow.user_id)
        ).scalars()
        return tuple(
            Enrollment(
                tenant_id=TenantId(row.tenant_id),
                course_id=CourseId(row.course_id),
                user_id=UserId(row.user_id),
                role=Role(row.role),
            )
            for row in rows
        )


def _user(row: UserRow | None) -> User | None:
    if row is None:
        return None
    return User(
        id=UserId(row.id),
        tenant_id=TenantId(row.tenant_id),
        login=row.login,
        display_name=row.display_name,
        email=row.email,
        password_hash=row.password_hash,
        state=UserState(row.state),
        created_at=row.created_at,
    )


def _api_token(row: ApiTokenRow | None) -> ApiToken | None:
    if row is None:
        return None
    return ApiToken(
        id=ApiTokenId(row.id),
        tenant_id=TenantId(row.tenant_id),
        user_id=UserId(row.user_id),
        token_hash=row.token_hash,
        note=row.note,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


def _course(row: CourseRow | None) -> Course | None:
    if row is None:
        return None
    return Course(
        id=CourseId(row.id),
        tenant_id=TenantId(row.tenant_id),
        code=row.code,
        title=row.title,
        term=row.term,
        subject_profile=row.subject_profile,
        auto_finalize_after_hours=row.auto_finalize_after_hours,
    )
