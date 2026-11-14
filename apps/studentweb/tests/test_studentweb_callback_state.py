"""Google ログインのコールバックが state を確かめること（学習者側、#688）。

既存のコールバックのテストは `_StubOidcProvider` で差し替えていて、そのスタブは
`exchange_code` の引数を見ない ── state の比較はアプリのテストでは一度も
実行されていなかった。ここでは **本物の `GoogleOidcProvider`** に
`httpx.MockTransport` で偽の Google を積み、正しい鍵で署名した ID トークンを
返させる。落ちる理由を state だけに絞るため、nonce はログイン開始時に
Cookie に入った値をそのままトークンに載せる。
"""

from __future__ import annotations

import time

import httpx
import pytest
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import RSAKey
from test_studentweb import World, _a_google_settings
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_identity.oidc import GoogleOidcProvider
from aijudge_persistence import ENV_OIDC_SECRET_KEY
from aijudge_studentweb import SESSION_COOKIE

STATE_COOKIE = "aijudge_oidc_state"
KID = "callback-kid"
_KEY = RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)
_ONE_HOUR = 3600


class _FakeGoogle:
    """トークン・JWKS エンドポイントの代わり。トークン交換に来たかを数える。"""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        #: ID トークンに載せる nonce。既定の空文字は「Cookie が無いときの期待値」と
        #: 同じ ── state の番が外れたら、そのままログインが通ってしまう値である。
        self.nonce = ""
        self.token_requests = 0

    def provider(self) -> GoogleOidcProvider:
        client = httpx.Client(transport=httpx.MockTransport(self._handle))
        return GoogleOidcProvider(http_client=client)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            self.token_requests += 1
            return httpx.Response(200, json={"id_token": self._id_token()})
        if "certs" in request.url.path:
            return httpx.Response(200, json={"keys": [_KEY.as_dict(private=False)]})
        raise AssertionError(f"unexpected request: {request.url}")

    def _id_token(self) -> str:
        now = int(time.time())
        payload = {
            "iss": "https://accounts.google.com",
            "aud": self.client_id,
            "sub": "sub-1",
            "email": "taro@example.ac.jp",
            "email_verified": True,
            "hd": "example.ac.jp",
            "nonce": self.nonce,
            "iat": now,
            "exp": now + _ONE_HOUR,
        }
        return jwt.encode({"alg": "RS256", "kid": KID}, payload, _KEY)


@pytest.fixture
def google(world: World, monkeypatch: pytest.MonkeyPatch) -> _FakeGoogle:
    import aijudge_studentweb.app as student_app

    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))
    settings = _a_google_settings()
    with world.database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(settings)
        uow.commit()
    fake = _FakeGoogle(settings.client_id)
    monkeypatch.setattr(student_app, "GoogleOidcProvider", fake.provider)
    return fake


def _start_login(world: World, google: _FakeGoogle) -> str:
    """ログインを始め、Cookie に入った state を返す（nonce は偽の Google に渡す）。"""
    started = world.client.get("/auth/login", follow_redirects=False)
    assert started.status_code == 303
    cookie = started.cookies[STATE_COOKIE]
    world.client.cookies.set(STATE_COOKIE, cookie)
    state, _, nonce = cookie.partition(":")
    google.nonce = nonce
    return state


def test_a_matching_state_logs_in(world: World, google: _FakeGoogle) -> None:
    """対照。下の 401 が偽の Google の組み立てのせいではないこと。"""
    state = _start_login(world, google)

    callback = world.client.get(
        "/auth/callback", params={"code": "auth-code", "state": state}, follow_redirects=False
    )

    assert callback.status_code == 303
    assert SESSION_COOKIE in callback.cookies


def test_a_callback_without_the_state_cookie_is_refused(world: World, google: _FakeGoogle) -> None:
    """**state の Cookie が無い要求は、Google に問い合わせる前に断る。**

    `exchange_code` の state 比較は `compare_digest` だけで、空文字どうしは一致する。
    URL にも state を付けない要求がここを抜けると、state の検査は素通りになる
    ── 偽の Google は nonce が空のトークンを返すので、そのままログインが通る。
    """
    callback = world.client.get(
        "/auth/callback", params={"code": "auth-code"}, follow_redirects=False
    )

    assert callback.status_code == 401
    assert SESSION_COOKIE not in callback.cookies
    assert google.token_requests == 0


def test_a_state_that_differs_from_the_cookie_is_refused(world: World, google: _FakeGoogle) -> None:
    """Cookie の state と URL の state が違えば断る（ログイン CSRF の防止）。

    nonce は Cookie と同じ値をトークンに載せてあるので、落ちる理由は state だけ。
    """
    state = _start_login(world, google)

    callback = world.client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": state + "-forged"},
        follow_redirects=False,
    )

    assert callback.status_code == 401
    assert SESSION_COOKIE not in callback.cookies
