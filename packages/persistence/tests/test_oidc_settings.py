"""OIDC 設定・Google ログインが SQL 実装でも同じ規則を守ることを確かめる（#124）。

`packages/identity/tests/test_oidc.py` がインメモリで規則を固定している。
ここで確かめるのは persistence 層に固有のこと ── `client_secret` の暗号化と、
`external_id` によるトランザクションを跨いだ利用者解決。**大学固有の値は
書かない**（ドメインは架空値のみ）。
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from aijudge_core.ids import TenantId
from aijudge_identity import AuthService, GoogleOidcIdentity
from aijudge_identity.oidc import OidcSettings
from aijudge_persistence import ENV_OIDC_SECRET_KEY, Database
from aijudge_persistence.schema import OidcSettingsRow

TENANT = TenantId("ten_" + "0" * 32)


@pytest.fixture
def database():
    db = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    yield db
    db.dispose()


@pytest.fixture(autouse=True)
def oidc_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))


def a_settings(**overrides: object) -> OidcSettings:
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "client_id": "client-abc",
        "client_secret": "shh-do-not-log-me",
        "allowed_domains": ("example.ac.jp",),
    }
    fields.update(overrides)
    return OidcSettings(**fields)  # type: ignore[arg-type]


def test_oidc_settings_round_trip_through_sql(database: Database) -> None:
    with database.unit_of_work() as uow:
        assert uow.identity.get_oidc_settings(TENANT) is None
        uow.identity.save_oidc_settings(a_settings())
        uow.commit()

    with database.unit_of_work() as uow:
        restored = uow.identity.get_oidc_settings(TENANT)

    assert restored == a_settings()


def test_the_client_secret_is_never_stored_as_plaintext(database: Database) -> None:
    with database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(a_settings())
        uow.commit()
        row = uow.session.get(OidcSettingsRow, str(TENANT))
        assert row is not None
        assert "shh-do-not-log-me" not in row.client_secret_encrypted


def test_saving_without_the_encryption_key_is_refused(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_OIDC_SECRET_KEY, raising=False)
    with database.unit_of_work() as uow, pytest.raises(RuntimeError, match=ENV_OIDC_SECRET_KEY):
        uow.identity.save_oidc_settings(a_settings())


def test_updating_settings_replaces_the_secret(database: Database) -> None:
    with database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(a_settings())
        uow.identity.save_oidc_settings(a_settings(client_secret="a-new-secret"))
        uow.commit()

    with database.unit_of_work() as uow:
        restored = uow.identity.get_oidc_settings(TENANT)

    assert restored is not None
    assert restored.client_secret == "a-new-secret"


def test_google_login_creates_and_then_resolves_the_same_user_across_transactions(
    database: Database,
) -> None:
    identity = GoogleOidcIdentity(sub="sub-1", email="taro@example.ac.jp", hd="example.ac.jp")

    with database.unit_of_work() as uow:
        principal1, _ = AuthService(uow.identity, audit=uow.audit).login_with_google(
            tenant_id=TENANT, identity=identity
        )
        uow.commit()

    with database.unit_of_work() as uow:
        principal2, token = AuthService(uow.identity, audit=uow.audit).login_with_google(
            tenant_id=TENANT, identity=identity
        )
        uow.commit()

    assert principal1.user_id == principal2.user_id

    with database.unit_of_work() as uow:
        assert AuthService(uow.identity, audit=uow.audit).resolve(token) == principal2
