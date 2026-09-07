"""Google OIDC ログインアダプタの規則を固定する（#121・#124）。

**大学固有の値は書かない。** ドメインは `example.ac.jp` のような架空値だけを使う。
Google のトークン・JWKS エンドポイントは `httpx.MockTransport` で模擬し、
自前の RSA 鍵で署名した ID トークンを検証させる ── 署名検証そのものは
`joserfc` に任せ、ここでは配線（state/nonce/ドメイン制限）だけを確かめる。
"""

from __future__ import annotations

import time

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from aijudge_core.ids import TenantId
from aijudge_identity import AuthenticationFailed, AuthService, InMemoryIdentityRepository
from aijudge_identity.oidc import GoogleOidcIdentity, GoogleOidcProvider, OidcSettings

TENANT = TenantId("ten_" + "0" * 32)
CLIENT_ID = "test-client-id"
REDIRECT_URI = "https://judge.example.ac.jp/auth/callback"
KID = "test-kid"

_KEY = RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)


def a_settings(*, allowed_domains: tuple[str, ...] = ("example.ac.jp",)) -> OidcSettings:
    return OidcSettings(
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        client_secret="test-client-secret",
        allowed_domains=allowed_domains,
    )


def an_id_token(
    *,
    nonce: str,
    sub: str = "sub-1",
    email: str = "taro@example.ac.jp",
    hd: str | None = "example.ac.jp",
    aud: str = CLIENT_ID,
) -> str:
    header = {"alg": "RS256", "kid": KID}
    payload: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": aud,
        "sub": sub,
        "email": email,
        "nonce": nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    if hd is not None:
        payload["hd"] = hd
    return jwt.encode(header, payload, _KEY)


def a_provider(*, id_token: str | None, token_status: int = 200) -> GoogleOidcProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": id_token})
        if "certs" in request.url.path:
            return httpx.Response(200, json={"keys": [_KEY.as_dict(private=False)]})
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleOidcProvider(http_client=client)


def exchange(
    provider: GoogleOidcProvider, *, state: str = "s", nonce: str, actual_state: str = "s"
):
    return provider.exchange_code(
        a_settings(),
        code="auth-code",
        redirect_uri=REDIRECT_URI,
        expected_state=state,
        actual_state=actual_state,
        expected_nonce=nonce,
    )


# --------------------------------------------------------------------------
# GoogleOidcProvider — state/nonce/ドメイン制限
# --------------------------------------------------------------------------


def test_a_successful_exchange_returns_the_verified_identity() -> None:
    provider = a_provider(id_token=an_id_token(nonce="nonce-1"))

    identity = exchange(provider, nonce="nonce-1")

    assert identity == GoogleOidcIdentity(
        sub="sub-1", email="taro@example.ac.jp", hd="example.ac.jp"
    )


def test_a_state_mismatch_is_rejected() -> None:
    provider = a_provider(id_token=an_id_token(nonce="n"))

    with pytest.raises(AuthenticationFailed):
        exchange(provider, state="expected", actual_state="different", nonce="n")


def test_a_nonce_mismatch_is_rejected() -> None:
    provider = a_provider(id_token=an_id_token(nonce="actual-nonce"))

    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="expected-nonce")


def test_a_token_endpoint_failure_is_rejected() -> None:
    provider = a_provider(id_token=None, token_status=400)

    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


def test_a_forged_signature_is_rejected() -> None:
    """別の鍵で署名したトークンは、JWKS の公開鍵と噛み合わず検証に失敗する。"""
    other_key = RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)
    forged = jwt.encode({"alg": "RS256", "kid": KID}, {"iss": "x", "aud": CLIENT_ID}, other_key)
    provider = a_provider(id_token=forged)

    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


def test_a_domain_outside_the_allowlist_is_rejected() -> None:
    provider = a_provider(
        id_token=an_id_token(nonce="n", email="taro@other.ac.jp", hd="other.ac.jp")
    )

    with pytest.raises(AuthenticationFailed):
        provider.exchange_code(
            a_settings(allowed_domains=("example.ac.jp",)),
            code="auth-code",
            redirect_uri=REDIRECT_URI,
            expected_state="s",
            actual_state="s",
            expected_nonce="n",
        )


def test_either_of_multiple_allowed_domains_can_log_in() -> None:
    """1 機関が複数ドメインを許すこともある（`allowed_domains` は単一値ではない）。"""
    settings = a_settings(allowed_domains=("example.ac.jp", "grad.example.ac.jp"))
    provider = a_provider(
        id_token=an_id_token(nonce="n", email="hanako@grad.example.ac.jp", hd="grad.example.ac.jp")
    )

    identity = provider.exchange_code(
        settings,
        code="auth-code",
        redirect_uri=REDIRECT_URI,
        expected_state="s",
        actual_state="s",
        expected_nonce="n",
    )

    assert identity.email == "hanako@grad.example.ac.jp"


# --------------------------------------------------------------------------
# AuthService.login_with_google — JIT プロビジョニングとセッション（#121）
# --------------------------------------------------------------------------


def an_identity(*, sub: str = "sub-1", email: str = "taro@example.ac.jp") -> GoogleOidcIdentity:
    return GoogleOidcIdentity(sub=sub, email=email, hd="example.ac.jp")


def test_first_login_creates_a_user_by_jit_provisioning() -> None:
    """事前の名簿投入は要らない（#121 で決定済み）。"""
    service = AuthService(InMemoryIdentityRepository())

    principal, token = service.login_with_google(tenant_id=TENANT, identity=an_identity())

    assert principal.login == "taro@example.ac.jp"
    assert token


def test_a_second_login_resolves_the_same_user() -> None:
    service = AuthService(InMemoryIdentityRepository())
    identity = an_identity()

    principal1, _ = service.login_with_google(tenant_id=TENANT, identity=identity)
    principal2, _ = service.login_with_google(tenant_id=TENANT, identity=identity)

    assert principal1.user_id == principal2.user_id


def test_a_google_session_resolves_like_any_other_session() -> None:
    """下流のコース・役割判定に変更が要らないことの確認。"""
    service = AuthService(InMemoryIdentityRepository())
    principal, token = service.login_with_google(tenant_id=TENANT, identity=an_identity())

    assert service.resolve(token) == principal


def test_oidc_settings_round_trip() -> None:
    """未設定テナントでは None ── ログイン画面の出し分けの根拠になる。"""
    repository = InMemoryIdentityRepository()
    assert repository.get_oidc_settings(TENANT) is None

    settings = a_settings()
    repository.save_oidc_settings(settings)

    assert repository.get_oidc_settings(TENANT) == settings
