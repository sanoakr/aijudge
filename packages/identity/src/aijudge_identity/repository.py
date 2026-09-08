"""利用者・セッション・受講の保存先（S1）。

保存先の実装は持たない。インメモリ実装はテストと開発のためのもので、
PostgreSQL 実装は persistence 側にある。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core import Course, Enrollment, term_sort_key
from aijudge_core.ids import ApiTokenId, CourseId, TenantId, UserId

from .models import ApiToken, Session, User
from .oidc import OidcSettings


@runtime_checkable
class IdentityRepository(Protocol):
    # -- 利用者 --
    def save_user(self, user: User) -> None: ...

    def get_user(self, user_id: UserId) -> User | None: ...

    def find_user_by_login(self, tenant_id: TenantId, login: str) -> User | None: ...

    def find_user_by_external_id(self, tenant_id: TenantId, external_id: str) -> User | None:
        """Google の `sub` から利用者を引く（#124）。JIT 突合の要。"""
        ...

    def list_all_users(self, tenant_id: TenantId) -> tuple[User, ...]:
        """このテナントの全利用者（#144）。

        `list_users(tenant_id, logins)` は受講者一覧のために「この login を
        まとめて引く」もので、名簿を持たない管理画面からは呼べない。
        """
        ...

    # -- OIDC 設定（テナント単位、#124）--
    def save_oidc_settings(self, settings: OidcSettings) -> None: ...

    def get_oidc_settings(self, tenant_id: TenantId) -> OidcSettings | None:
        """未設定なら None ── ログイン画面はこれで Google ボタンの出し分けをする。"""
        ...

    # -- セッション --
    def save_session(self, session: Session) -> None: ...

    def find_session_by_token_hash(self, token_hash: str) -> Session | None: ...

    def revoke_session(self, session_id: str, at: datetime) -> None: ...

    # -- API トークン --

    def save_api_token(self, token: ApiToken) -> None: ...

    def find_api_token_by_hash(self, token_hash: str) -> ApiToken | None: ...

    def touch_api_token(self, token_id: ApiTokenId, at: datetime) -> None:
        """最終使用日時を記録する。使われていないトークンを見つけるため。"""
        ...

    def revoke_api_token(self, token_id: ApiTokenId, at: datetime) -> None: ...

    def list_api_tokens(self, tenant_id: TenantId) -> tuple[ApiToken, ...]:
        """発行済みトークン。**ハッシュしか持たないので平文は出ない。**"""
        ...

    def revoke_sessions_for(self, user_id: UserId, at: datetime) -> None:
        """この利用者の全セッションを切る。

        パスワード変更と無効化で使う。乗っ取られていた場合の復旧手段が
        これしかない。
        """
        ...

    # -- コースと受講 --
    def save_course(self, course: Course) -> None: ...

    def get_course(self, course_id: CourseId) -> Course | None: ...

    def save_enrollment(self, enrollment: Enrollment) -> None: ...

    def find_enrollment(self, course_id: CourseId, user_id: UserId) -> Enrollment | None: ...

    def list_courses_for_user(self, tenant_id: TenantId, user_id: UserId) -> tuple[Course, ...]: ...

    def list_courses(self, tenant_id: TenantId) -> tuple[Course, ...]:
        """このテナントの全コース。**テナント管理者の一覧表示に使う**（#128）。

        管理者は受講登録なしで全コースに届く（`AuthService.role_in`）ので、
        「自分が受講登録されているコース」だけでは何も出せない。
        """
        ...

    def delete_course(self, course_id: CourseId) -> None:
        """コースと、その受講登録を消す（#156）。

        **提出が無いことは呼び出し側が確かめる**（`aijudge_admin.courses`）。
        保存層は言われたものを消す ── 規則の置き場所を 1 つにするため
        （`delete_task` と同じ分担）。

        受講登録も一緒に消す。`enrollments` はコースへの外部キーを持つので、
        残すと消せない。**利用者は消さない**（他のコースにも居る）。
        """
        ...

    def list_courses_using_profile(self, subject_profile: str) -> tuple[Course, ...]:
        """この科目プロファイルを参照しているコース。**テナントを越えて調べる。**

        `subjects/` はデプロイ全体で 1 つの共有ディレクトリで、テナント別に
        分かれていない。だから「このプロファイルを書き換えてよいか」は
        テナント内だけを見ても決められない ── 他のテナントのコースが
        参照していれば、その採点が変わる。
        """
        ...

    def list_enrollments(self, course_id: CourseId) -> tuple[Enrollment, ...]: ...

    def remove_enrollment(self, course_id: CourseId, user_id: UserId) -> None:
        """受講を取り消す。

        **利用者は消さない。** 過去の提出と採点が参照しているので、消すと
        成績の履歴が壊れる（`AuthService.disable` と同じ理屈）。
        """
        ...

    def list_users(self, tenant_id: TenantId, logins: tuple[str, ...]) -> tuple[User, ...]:
        """login をまとめて引く。受講者一覧の表示に使う。"""
        ...


class InMemoryIdentityRepository:
    """テストと開発用。"""

    def __init__(self) -> None:
        self._users: dict[UserId, User] = {}
        self._logins: dict[tuple[TenantId, str], UserId] = {}
        self._sessions: dict[str, Session] = {}
        self._by_token: dict[str, str] = {}
        self._api_tokens: dict[ApiTokenId, ApiToken] = {}
        self._courses: dict[CourseId, Course] = {}
        self._enrollments: dict[tuple[CourseId, UserId], Enrollment] = {}
        self._by_external_id: dict[tuple[TenantId, str], UserId] = {}
        self._oidc_settings: dict[TenantId, OidcSettings] = {}

    def save_user(self, user: User) -> None:
        self._users[user.id] = user
        self._logins[(user.tenant_id, user.login)] = user.id
        if user.external_id is not None:
            self._by_external_id[(user.tenant_id, user.external_id)] = user.id

    def get_user(self, user_id: UserId) -> User | None:
        return self._users.get(user_id)

    def find_user_by_login(self, tenant_id: TenantId, login: str) -> User | None:
        user_id = self._logins.get((tenant_id, login))
        return None if user_id is None else self._users.get(user_id)

    def find_user_by_external_id(self, tenant_id: TenantId, external_id: str) -> User | None:
        user_id = self._by_external_id.get((tenant_id, external_id))
        return None if user_id is None else self._users.get(user_id)

    def list_all_users(self, tenant_id: TenantId) -> tuple[User, ...]:
        return tuple(
            sorted(
                (user for user in self._users.values() if user.tenant_id == tenant_id),
                key=lambda user: user.login,
            )
        )

    def save_oidc_settings(self, settings: OidcSettings) -> None:
        self._oidc_settings[settings.tenant_id] = settings

    def get_oidc_settings(self, tenant_id: TenantId) -> OidcSettings | None:
        return self._oidc_settings.get(tenant_id)

    def save_session(self, session: Session) -> None:
        self._sessions[session.id] = session
        self._by_token[session.token_hash] = session.id

    def find_session_by_token_hash(self, token_hash: str) -> Session | None:
        session_id = self._by_token.get(token_hash)
        return None if session_id is None else self._sessions.get(session_id)

    def revoke_session(self, session_id: str, at: datetime) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            self._sessions[session_id] = session.model_copy(update={"revoked_at": at})

    def revoke_sessions_for(self, user_id: UserId, at: datetime) -> None:
        for session_id, session in list(self._sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self._sessions[session_id] = session.model_copy(update={"revoked_at": at})

    def save_api_token(self, token: ApiToken) -> None:
        self._api_tokens[token.id] = token

    def find_api_token_by_hash(self, token_hash: str) -> ApiToken | None:
        for token in self._api_tokens.values():
            if token.token_hash == token_hash:
                return token
        return None

    def touch_api_token(self, token_id: ApiTokenId, at: datetime) -> None:
        token = self._api_tokens.get(token_id)
        if token is not None:
            self._api_tokens[token_id] = token.model_copy(update={"last_used_at": at})

    def revoke_api_token(self, token_id: ApiTokenId, at: datetime) -> None:
        token = self._api_tokens.get(token_id)
        if token is not None:
            self._api_tokens[token_id] = token.model_copy(update={"revoked_at": at})

    def list_api_tokens(self, tenant_id: TenantId) -> tuple[ApiToken, ...]:
        return tuple(
            sorted(
                (t for t in self._api_tokens.values() if t.tenant_id == tenant_id),
                key=lambda t: t.created_at,
            )
        )

    def save_course(self, course: Course) -> None:
        self._courses[course.id] = course

    def get_course(self, course_id: CourseId) -> Course | None:
        return self._courses.get(course_id)

    def save_enrollment(self, enrollment: Enrollment) -> None:
        self._enrollments[(enrollment.course_id, enrollment.user_id)] = enrollment

    def find_enrollment(self, course_id: CourseId, user_id: UserId) -> Enrollment | None:
        return self._enrollments.get((course_id, user_id))

    def list_courses_for_user(self, tenant_id: TenantId, user_id: UserId) -> tuple[Course, ...]:
        course_ids = [
            course_id
            for (course_id, member), enrollment in self._enrollments.items()
            if member == user_id and enrollment.tenant_id == tenant_id
        ]
        return tuple(
            sorted(
                (self._courses[cid] for cid in course_ids if cid in self._courses),
                key=lambda course: (term_sort_key(course.term), course.code),
            )
        )

    def list_courses(self, tenant_id: TenantId) -> tuple[Course, ...]:
        return tuple(
            sorted(
                (c for c in self._courses.values() if c.tenant_id == tenant_id),
                key=lambda course: (term_sort_key(course.term), course.code),
            )
        )

    def delete_course(self, course_id: CourseId) -> None:
        self._courses.pop(course_id, None)
        for key in [key for key in self._enrollments if key[0] == course_id]:
            del self._enrollments[key]

    def list_courses_using_profile(self, subject_profile: str) -> tuple[Course, ...]:
        return tuple(
            sorted(
                (c for c in self._courses.values() if c.subject_profile == subject_profile),
                key=lambda course: (term_sort_key(course.term), course.code),
            )
        )

    def list_enrollments(self, course_id: CourseId) -> tuple[Enrollment, ...]:
        return tuple(
            enrollment for (cid, _), enrollment in self._enrollments.items() if cid == course_id
        )

    def remove_enrollment(self, course_id: CourseId, user_id: UserId) -> None:
        self._enrollments.pop((course_id, user_id), None)

    def list_users(self, tenant_id: TenantId, logins: tuple[str, ...]) -> tuple[User, ...]:
        wanted = set(logins)
        return tuple(
            user
            for user in self._users.values()
            if user.tenant_id == tenant_id and user.login in wanted
        )
