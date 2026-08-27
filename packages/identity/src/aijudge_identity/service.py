"""認証（S1）。

Phase 0 の範囲はローカル DB 認証だけ。SSO（学認/SAML・OIDC）と LTI 1.3 は
Phase 8 で、**この層のアダプタとして足す**。だから外向きの型
（`Principal`）は認証方式を含まない。

守ること:

- 平文のパスワードを保存しない・ログに出さない
- 利用者の存在を応答から推測させない（存在しない login でも同じだけ時間を使う）
- セッショントークンは DB にハッシュで持つ（漏洩しても乗っ取れない）
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from aijudge_core import Course, Enrollment, Role
from aijudge_core.ids import CourseId, SessionId, TenantId, UserId, new_id

from .models import Principal, Session, User, UserState
from .passwords import hash_password, needs_rehash, verify_password
from .repository import IdentityRepository

# セッションの有効期間。学生が 1 コマの授業中に切れない程度、かつ
# 共用端末に置き去りにされたまま延々と生きない程度。
DEFAULT_SESSION_HOURS = 12
TOKEN_BYTES = 32

# 存在しない login に対しても検証を走らせるためのダミー。
# 応答時間の差で「その ID は存在する」と分かってしまうのを防ぐ。
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-comparison")


class AuthenticationFailed(Exception):
    """認証に失敗した。

    **理由を分けない。** 「利用者が居ない」と「パスワードが違う」を
    分けて返すと、有効な ID の一覧を作れてしまう。
    """


class PermissionDenied(Exception):
    """権限が無い。"""


class AuthService:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        session_hours: int = DEFAULT_SESSION_HOURS,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._session_hours = session_hours

    # -- 利用者 ------------------------------------------------------------

    def register(
        self,
        *,
        tenant_id: TenantId,
        login: str,
        display_name: str,
        password: str,
        email: str | None = None,
    ) -> Principal:
        if self._repository.find_user_by_login(tenant_id, login) is not None:
            raise AuthenticationFailed("この ID は既に使われています")
        user = User(
            id=UserId(new_id("usr")),
            tenant_id=tenant_id,
            login=login,
            display_name=display_name,
            email=email,
            password_hash=hash_password(password),
            created_at=self._clock(),
        )
        self._repository.save_user(user)
        return _principal(user)

    def change_password(self, user_id: UserId, *, current: str, new: str) -> None:
        user = self._repository.get_user(user_id)
        if user is None or not verify_password(current, user.password_hash):
            raise AuthenticationFailed("現在のパスワードが違います")
        self._repository.save_user(user.model_copy(update={"password_hash": hash_password(new)}))
        # パスワードを変えたら既存のセッションを切る。乗っ取られていた場合の
        # 復旧手段がこれしかない。
        self._repository.revoke_sessions_for(user_id, self._clock())

    def disable(self, user_id: UserId) -> None:
        """利用者を無効化する。**削除しない。**

        過去の提出と採点が参照しているので、消すと成績の履歴が壊れる。
        """
        user = self._repository.get_user(user_id)
        if user is None:
            return
        self._repository.save_user(user.model_copy(update={"state": UserState.DISABLED}))
        self._repository.revoke_sessions_for(user_id, self._clock())

    # -- ログイン ----------------------------------------------------------

    def login(self, *, tenant_id: TenantId, login: str, password: str) -> tuple[Principal, str]:
        """認証してセッションを作る。返すのは主体と**平文トークン**。

        平文トークンを返すのはこの 1 回だけ。保存するのはハッシュなので、
        あとから取り出す方法は無い。
        """
        user = self._repository.find_user_by_login(tenant_id, login)
        if user is None:
            # 存在しない ID でも同じだけ時間を使う。
            verify_password(password, _DUMMY_HASH)
            raise AuthenticationFailed("ID またはパスワードが違います")
        if not verify_password(password, user.password_hash):
            raise AuthenticationFailed("ID またはパスワードが違います")
        if not user.is_active:
            raise AuthenticationFailed("この利用者は無効化されています")

        if needs_rehash(user.password_hash):
            # パラメータを上げた後、次のログインで自動的に強い方へ移す。
            user = user.model_copy(update={"password_hash": hash_password(password)})
            self._repository.save_user(user)

        now = self._clock()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._repository.save_session(
            Session(
                id=SessionId(new_id("ses")),
                user_id=user.id,
                tenant_id=tenant_id,
                token_hash=_token_hash(token),
                created_at=now,
                expires_at=now + timedelta(hours=self._session_hours),
            )
        )
        return _principal(user), token

    def resolve(self, token: str) -> Principal | None:
        """トークンから主体を引く。無効なら None。"""
        if not token:
            return None
        session = self._repository.find_session_by_token_hash(_token_hash(token))
        if session is None or not session.is_valid(self._clock()):
            return None
        user = self._repository.get_user(session.user_id)
        if user is None or not user.is_active:
            return None
        return _principal(user)

    def logout(self, token: str) -> None:
        session = self._repository.find_session_by_token_hash(_token_hash(token))
        if session is not None:
            self._repository.revoke_session(session.id, self._clock())

    # -- コースと受講 ------------------------------------------------------

    def enroll(
        self, *, tenant_id: TenantId, course_id: CourseId, user_id: UserId, role: Role
    ) -> Enrollment:
        enrollment = Enrollment(
            tenant_id=tenant_id, course_id=course_id, user_id=user_id, role=role
        )
        self._repository.save_enrollment(enrollment)
        return enrollment

    def role_in(self, course_id: CourseId, user_id: UserId) -> Role | None:
        enrollment = self._repository.find_enrollment(course_id, user_id)
        return None if enrollment is None else enrollment.role

    def require_membership(self, course_id: CourseId, user_id: UserId) -> Role:
        """このコースの一員であることを要求する。

        「見えてはいけないものが見えない」の最後の砦。UI で隠すのは
        表示の都合であって権限ではない。
        """
        role = self.role_in(course_id, user_id)
        if role is None:
            raise PermissionDenied("このコースの受講者ではありません")
        return role

    def require_grader(self, course_id: CourseId, user_id: UserId) -> Role:
        role = self.require_membership(course_id, user_id)
        enrollment = self._repository.find_enrollment(course_id, user_id)
        if enrollment is None or not enrollment.can_grade:
            raise PermissionDenied("採点の権限がありません")
        return role

    def courses_for(self, tenant_id: TenantId, user_id: UserId) -> tuple[Course, ...]:
        return self._repository.list_courses_for_user(tenant_id, user_id)


def _principal(user: User) -> Principal:
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        login=user.login,
        display_name=user.display_name,
    )


def _token_hash(token: str) -> str:
    """トークンのハッシュ。

    ソルトは付けない。トークンは 256 ビットの乱数なので辞書攻撃の対象に
    ならず、ソルトを付けるとハッシュから引けなくなる（引くのが目的）。
    """
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
