"""認証の結果が監査ログに残ること（ADR 0016）。

`AuthService` が自分で書くのは**認証の結果だけ**である。トークンの発行や
受講の付与は呼び出し側が書く ── 操作しているのは対象の利用者ではなく教員や
管理者で、それを知っているのは呼び出し側だけだからで、ここで書くと
**操作された人が操作した人として記録される。**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_audit import ActorKind, AuditAction, InMemoryAuditLog
from aijudge_core.ids import TenantId
from aijudge_identity import AuthenticationFailed, AuthService
from aijudge_identity.repository import InMemoryIdentityRepository

TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def a_service(audit: InMemoryAuditLog) -> AuthService:
    return AuthService(
        InMemoryIdentityRepository(),
        audit=audit,
        clock=lambda: NOW,
        request_id="req-1",
        source_ip="203.0.113.7",
    )


def _registered(service: AuthService, login: str = "s2400001"):
    return service.register(tenant_id=TENANT, login=login, display_name="学生 A", password=PASSWORD)


def test_a_successful_login_is_recorded() -> None:
    audit = InMemoryAuditLog()
    service = a_service(audit)
    user = _registered(service)

    service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)

    (row,) = audit.list_recent(TENANT, action=AuditAction.LOGIN_SUCCEEDED)
    assert row.actor_kind is ActorKind.USER
    assert row.actor_user_id == user.user_id
    assert row.detail["method"] == "password"
    # 運用ログと突き合わせるための鍵（ADR 0016）。
    assert row.request_id == "req-1"
    assert row.source_ip == "203.0.113.7"


def test_a_failed_login_is_not_attributed_to_the_account_holder() -> None:
    """パスワードを間違えたのが本人とは限らない。

    **むしろ本人でない場合こそ記録が要る。** 対象は口座、操作者は不明。
    """
    audit = InMemoryAuditLog()
    service = a_service(audit)
    user = _registered(service)

    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=TENANT, login="s2400001", password="wrong")

    (row,) = audit.list_recent(TENANT, action=AuditAction.LOGIN_FAILED)
    assert row.actor_kind is ActorKind.ANONYMOUS
    assert row.actor_user_id is None
    # 口座は分かっている。分かっていることと、やった人が分かることは別。
    assert row.target_id == str(user.user_id)
    assert row.detail["reason"] == "bad password"


def test_the_record_distinguishes_a_missing_account_from_a_wrong_password() -> None:
    """応答は同じにするが（ID の列挙を防ぐ）、**記録には差を書く。**

    区別できないと、「存在しない ID への総当たり」と「特定の口座への総当たり」を
    後から読み分けられない。攻撃の形が違えば取るべき手も違う。
    """
    audit = InMemoryAuditLog()
    service = a_service(audit)
    _registered(service)

    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=TENANT, login="nobody", password=PASSWORD)
    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=TENANT, login="s2400001", password="wrong")

    reasons = [row.detail["reason"] for row in audit.list_recent(TENANT)]
    assert set(reasons) == {"no such user", "bad password"}
    unknown = next(
        row for row in audit.list_recent(TENANT) if row.detail["reason"] == "no such user"
    )
    assert unknown.target_id == "unknown"


def test_a_disabled_account_records_why_it_was_refused() -> None:
    audit = InMemoryAuditLog()
    service = a_service(audit)
    user = _registered(service)
    service.disable(user.user_id)

    with pytest.raises(AuthenticationFailed):
        service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)

    (row,) = audit.list_recent(TENANT, action=AuditAction.LOGIN_FAILED)
    assert row.detail["reason"] == "disabled"


def test_logging_out_is_recorded() -> None:
    audit = InMemoryAuditLog()
    service = a_service(audit)
    user = _registered(service)
    _, token = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)

    service.logout(token)

    (row,) = audit.list_recent(TENANT, action=AuditAction.LOGGED_OUT)
    assert row.actor_user_id == user.user_id


def test_logging_out_an_expired_session_records_nothing() -> None:
    """起きなかったことを書かない。

    書くと「誰かがログアウトした」行が実体なく積もり、監査の信号が薄まる。
    """
    audit = InMemoryAuditLog()
    service = a_service(audit)
    _registered(service)

    service.logout("not-a-real-token")

    assert audit.list_recent(TENANT, action=AuditAction.LOGGED_OUT) == ()
