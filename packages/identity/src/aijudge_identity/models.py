"""利用者とセッション（S1）。

コアには `Tenant` / `Course` / `Enrollment` / `Role` の語彙が既にある
（core/tenancy.py）。ここに置くのは、**認証に固有で採点に不要なもの**だけ。
採点側が利用者の資格情報を知る必要はない。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import SessionId, TenantId, UserId


class UserState(StrEnum):
    ACTIVE = "active"
    # 退学・異動。削除しない（過去の提出と採点が参照している）。
    DISABLED = "disabled"


class User(BaseModel):
    """利用者。

    `password_hash` をこの型に持たせているのは、認証が S1 の内部で完結する
    ようにするため。**この型を S1 の外に出さない。** 外向きには
    `Principal` を渡す。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UserId
    tenant_id: TenantId
    # ログイン識別子。学籍番号でもメールでもよいので用途を固定しない。
    login: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    email: str | None = None
    password_hash: str = Field(min_length=1)
    state: UserState = UserState.ACTIVE
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.state is UserState.ACTIVE


class Principal(BaseModel):
    """認証済みの主体。**資格情報を含まない。**

    アプリ層に渡るのはこれ。`User` をそのまま渡すと、テンプレートや
    ログにパスワードハッシュが漏れる経路ができる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UserId
    tenant_id: TenantId
    login: str
    display_name: str


class Session(BaseModel):
    """ログインセッション。

    **トークンそのものは保存しない。** 保存するのはトークンのハッシュで、
    DB が漏れてもセッションを乗っ取れないようにするため（パスワードと同じ理屈）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: SessionId
    user_id: UserId
    tenant_id: TenantId
    token_hash: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_valid(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
