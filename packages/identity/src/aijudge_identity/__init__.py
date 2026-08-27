"""aiJudge identity (S1) — ローカル認証、コース、受講。

Phase 0 の範囲はローカル DB 認証だけ。SSO（学認/SAML・OIDC）と LTI 1.3 は
Phase 8 で、**この層のアダプタとして足す**。外向きの型（`Principal`）が
認証方式を含まないのはそのため。

`User` は S1 の外に出さない。パスワードハッシュを持つので、外に出すと
テンプレートやログに漏れる経路ができる。
"""

from __future__ import annotations

from .models import Principal, Session, User, UserState
from .passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    hash_password,
    needs_rehash,
    verify_password,
)
from .repository import IdentityRepository, InMemoryIdentityRepository
from .service import (
    DEFAULT_SESSION_HOURS,
    AuthenticationFailed,
    AuthService,
    PermissionDenied,
)

__all__ = [
    "DEFAULT_SESSION_HOURS",
    "MIN_PASSWORD_LENGTH",
    "AuthService",
    "AuthenticationFailed",
    "IdentityRepository",
    "InMemoryIdentityRepository",
    "PermissionDenied",
    "Principal",
    "Session",
    "User",
    "UserState",
    "WeakPassword",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
