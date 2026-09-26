"""ローカルログインの総当たりを止める（#418）。

数えるのは監査ログの失敗の行。止めている間はパスワードを確かめず、
ID の有無に関わらず同じ応答を返す。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_audit import InMemoryAuditLog
from aijudge_core.ids import TenantId
from aijudge_identity import AuthenticationFailed, AuthService
from aijudge_identity.repository import InMemoryIdentityRepository
from aijudge_identity.service import (
    LOGIN_FAILURE_WINDOW_MINUTES,
    MAX_LOGIN_FAILURES_PER_ID,
    MAX_LOGIN_FAILURES_PER_SOURCE,
)

TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _service(repo, audit, clock, source_ip: str = "203.0.113.7") -> AuthService:
    return AuthService(repo, audit=audit, clock=clock, source_ip=source_ip)


def _fail(service: AuthService, login: str, times: int) -> None:
    for _ in range(times):
        with pytest.raises(AuthenticationFailed):
            service.login(tenant_id=TENANT, login=login, password="wrong")


def test_the_right_password_is_refused_after_too_many_failures() -> None:
    repo, audit, clock = InMemoryIdentityRepository(), InMemoryAuditLog(), Clock()
    service = _service(repo, audit, clock)
    service.register(tenant_id=TENANT, login="admin", display_name="管理者", password=PASSWORD)
    _fail(service, "admin", MAX_LOGIN_FAILURES_PER_ID)

    with pytest.raises(AuthenticationFailed, match="しばらく受け付けません"):
        service.login(tenant_id=TENANT, login="admin", password=PASSWORD)


def test_a_missing_account_gets_the_same_answer() -> None:
    """止めているかどうかで ID の有無が分からないこと。"""
    repo, audit, clock = InMemoryIdentityRepository(), InMemoryAuditLog(), Clock()
    service = _service(repo, audit, clock)
    _fail(service, "nobody", MAX_LOGIN_FAILURES_PER_ID)
    with pytest.raises(AuthenticationFailed, match="しばらく受け付けません"):
        service.login(tenant_id=TENANT, login="nobody", password="x")


def test_the_block_lifts_after_the_window() -> None:
    repo, audit, clock = InMemoryIdentityRepository(), InMemoryAuditLog(), Clock()
    service = _service(repo, audit, clock)
    service.register(tenant_id=TENANT, login="admin", display_name="管理者", password=PASSWORD)
    _fail(service, "admin", MAX_LOGIN_FAILURES_PER_ID)

    clock.now += timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES + 1)
    principal, _ = service.login(tenant_id=TENANT, login="admin", password=PASSWORD)
    assert principal.login == "admin"


def test_a_few_typos_do_not_lock_anyone_out() -> None:
    repo, audit, clock = InMemoryIdentityRepository(), InMemoryAuditLog(), Clock()
    service = _service(repo, audit, clock)
    service.register(tenant_id=TENANT, login="s2400001", display_name="学生", password=PASSWORD)
    _fail(service, "s2400001", 3)
    principal, _ = service.login(tenant_id=TENANT, login="s2400001", password=PASSWORD)
    assert principal.login == "s2400001"


def test_rotating_ids_from_one_source_is_stopped() -> None:
    repo, audit, clock = InMemoryIdentityRepository(), InMemoryAuditLog(), Clock()
    service = _service(repo, audit, clock)
    service.register(tenant_id=TENANT, login="admin", display_name="管理者", password=PASSWORD)
    for index in range(MAX_LOGIN_FAILURES_PER_SOURCE):
        _fail(service, f"guess{index}", 1)

    with pytest.raises(AuthenticationFailed, match="しばらく受け付けません"):
        service.login(tenant_id=TENANT, login="admin", password=PASSWORD)
    # 別の送信元（教室の別の PC）は止まらない。
    other = _service(repo, audit, clock, source_ip="198.51.100.9")
    principal, _ = other.login(tenant_id=TENANT, login="admin", password=PASSWORD)
    assert principal.login == "admin"
