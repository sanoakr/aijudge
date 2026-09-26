"""利用者・セッション・受講の保存先（S1）。

保存先の実装は持たない。インメモリ実装はテストと開発のためのもので、
PostgreSQL 実装は persistence 側にある。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core import Course, CourseGroup, Enrollment, Role, term_sort_key
from aijudge_core.ids import ApiTokenId, CourseGroupId, CourseId, TenantId, UserId

from .models import ApiToken, Session, User
from .network import CampusNetworkSettings
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

    # -- 学内ネットワーク（#333）--
    def save_campus_networks(self, settings: CampusNetworkSettings) -> None: ...

    def get_campus_networks(self, tenant_id: TenantId) -> CampusNetworkSettings | None:
        """未設定なら None。

        **空の設定と未設定を区別しない側で使う** ── どちらも範囲が無い状態で、
        制限は効かない（`CampusAccess.NOT_CONFIGURED`）。区別が要るのは
        設定画面だけで、そこは「保存したことがあるか」を別に訊かない。
        """
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

    def list_all_courses(self) -> tuple[Course, ...]:
        """全テナントの全コース。**運用者の処理が使う**（自動確定・KC の利用状況）。

        自動確定は運用者が cron で回すもので、テナントを 1 つずつ挙げさせると
        テナントを足したときに漏れる。KC はコースをまたいで共有されるので、
        利用状況も 1 コースやテナントに閉じては数えられない。

        以前は呼び出し側（`aijudge_admin`）が `CourseRow` を直接読んで `Course` を
        組み立てていた（段階的な立て直し 1-3）。その写しは遅延の減点・KC・
        ルーブリックの畳み方を読み落としていた ── 使う側がまだ読んでいなかった
        だけで、読み始めた日に黙って空になる形だった。
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

    def roles_by_user(self, tenant_id: TenantId) -> dict[UserId, frozenset[Role]]:
        """このテナントで、利用者ごとに**どの役割を持っているか**（#326）。

        役割はコースごとに付く（`Enrollment`）ので、テナント全体の一覧では
        1 人が複数の役割を持ちうる ── 同じ人が片方のコースの教員で、別の
        コースの TA であることは普通にある。**丸めない**：どちらかに決めると、
        「TA で絞る」がその人を落とす。

        受講の無い利用者は**鍵ごと現れない**。空集合を返すと「役割を持たない」
        と「そもそも受講が無い」が同じ形になり、呼び出し側で区別できない。
        """
        ...

    def remove_enrollment(self, course_id: CourseId, user_id: UserId) -> None:
        """受講を取り消す。

        **利用者は消さない。** 過去の提出と採点が参照しているので、消すと
        成績の履歴が壊れる（`AuthService.disable` と同じ理屈）。
        """
        ...

    def list_users(self, tenant_id: TenantId, logins: tuple[str, ...]) -> tuple[User, ...]:
        """login をまとめて引く。受講者一覧の表示に使う。"""
        ...

    # -- 出題先の名簿（`docs/design/task-visibility.md`） --
    #
    # **規則は持たない。** 名簿に入れてよいのが受講者だけであること、出題先
    # として使われているグループを消させないことは `aijudge_admin.groups` が
    # 確かめる。保存層は言われたものを保存する（`delete_course` と同じ分担）。

    def save_group(self, group: CourseGroup) -> None:
        """作る・名前を変える。名前はコース内で一意（同じ名前の別 ID は保存層が拒む）。"""
        ...

    def get_group(self, group_id: CourseGroupId) -> CourseGroup | None: ...

    def find_group(self, course_id: CourseId, name: str) -> CourseGroup | None:
        """名前で引く。API とスクリプトはグループを名前で指す。"""
        ...

    def list_groups(self, course_id: CourseId) -> tuple[CourseGroup, ...]:
        """このコースのグループ。**名前の順。**"""
        ...

    def delete_group(self, group_id: CourseGroupId) -> None:
        """グループとその名簿を消す。"""
        ...

    def set_group_members(self, group_id: CourseGroupId, user_ids: frozenset[UserId]) -> None:
        """名簿を**丸ごと置き換える**。足し引きの操作は持たない ── 同じ要求を
        2 度流しても結果が同じになる（API の冪等性はここから来る）。"""
        ...

    def group_members(self, group_id: CourseGroupId) -> frozenset[UserId]: ...

    def groups_of(self, course_id: CourseId, user_id: UserId) -> frozenset[CourseGroupId]:
        """この人がこのコースで入っているグループ。`may_see` に渡す。"""
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
        self._campus: dict[str, CampusNetworkSettings] = {}
        self._groups: dict[CourseGroupId, CourseGroup] = {}
        self._members: dict[CourseGroupId, frozenset[UserId]] = {}

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

    def list_all_courses(self) -> tuple[Course, ...]:
        return tuple(
            sorted(
                self._courses.values(),
                key=lambda course: (term_sort_key(course.term), course.code),
            )
        )

    def delete_course(self, course_id: CourseId) -> None:
        self._courses.pop(course_id, None)
        for key in [key for key in self._enrollments if key[0] == course_id]:
            del self._enrollments[key]
        for group in [g for g in self._groups.values() if g.course_id == course_id]:
            self.delete_group(group.id)

    def list_courses_using_profile(self, subject_profile: str) -> tuple[Course, ...]:
        return tuple(
            sorted(
                (c for c in self._courses.values() if c.subject_profile == subject_profile),
                key=lambda course: (term_sort_key(course.term), course.code),
            )
        )

    def save_campus_networks(self, settings: CampusNetworkSettings) -> None:
        self._campus[str(settings.tenant_id)] = settings

    def get_campus_networks(self, tenant_id: TenantId) -> CampusNetworkSettings | None:
        return self._campus.get(str(tenant_id))

    def list_enrollments(self, course_id: CourseId) -> tuple[Enrollment, ...]:
        return tuple(
            enrollment for (cid, _), enrollment in self._enrollments.items() if cid == course_id
        )

    def roles_by_user(self, tenant_id: TenantId) -> dict[UserId, frozenset[Role]]:
        found: dict[UserId, set[Role]] = {}
        for enrollment in self._enrollments.values():
            if enrollment.tenant_id != tenant_id:
                continue
            found.setdefault(enrollment.user_id, set()).add(enrollment.role)
        return {user_id: frozenset(roles) for user_id, roles in found.items()}

    def remove_enrollment(self, course_id: CourseId, user_id: UserId) -> None:
        self._enrollments.pop((course_id, user_id), None)

    def list_users(self, tenant_id: TenantId, logins: tuple[str, ...]) -> tuple[User, ...]:
        wanted = set(logins)
        return tuple(
            user
            for user in self._users.values()
            if user.tenant_id == tenant_id and user.login in wanted
        )

    def save_group(self, group: CourseGroup) -> None:
        clash = self.find_group(group.course_id, group.name)
        if clash is not None and clash.id != group.id:
            # SQL 実装の一意制約と同じ振る舞いにする。
            raise ValueError(f"group name {group.name!r} is already used in this course")
        self._groups[group.id] = group

    def get_group(self, group_id: CourseGroupId) -> CourseGroup | None:
        return self._groups.get(group_id)

    def find_group(self, course_id: CourseId, name: str) -> CourseGroup | None:
        wanted = name.strip()
        for group in self._groups.values():
            if group.course_id == course_id and group.name == wanted:
                return group
        return None

    def list_groups(self, course_id: CourseId) -> tuple[CourseGroup, ...]:
        return tuple(
            sorted(
                (g for g in self._groups.values() if g.course_id == course_id),
                key=lambda group: group.name,
            )
        )

    def delete_group(self, group_id: CourseGroupId) -> None:
        self._groups.pop(group_id, None)
        self._members.pop(group_id, None)

    def set_group_members(self, group_id: CourseGroupId, user_ids: frozenset[UserId]) -> None:
        self._members[group_id] = frozenset(user_ids)

    def group_members(self, group_id: CourseGroupId) -> frozenset[UserId]:
        return self._members.get(group_id, frozenset())

    def groups_of(self, course_id: CourseId, user_id: UserId) -> frozenset[CourseGroupId]:
        return frozenset(
            group_id
            for group_id, members in self._members.items()
            if user_id in members
            and (group := self._groups.get(group_id)) is not None
            and group.course_id == course_id
        )
