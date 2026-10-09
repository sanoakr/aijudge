"""セッションが切れたら、ログインし直して元のページへ戻る（教員コンソール）。

コンソールは逆プロキシの接頭辞（`/console`）の下に置かれる。戻り先は
接頭辞なしで持ち、`Location` を作るときに接頭辞を足す ── 二重にも欠けもしない
ことをここで見る。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from test_console import COURSE, PASSWORD, World, _a_google_settings, _StubOidcProvider
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_identity import GoogleOidcIdentity
from aijudge_persistence import ENV_OIDC_SECRET_KEY
from aijudge_reviewconsole import ENV_ROOT_PREFIX, SESSION_COOKIE
from aijudge_webapp import NEXT_COOKIE

PAGE = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
LIST = f"/courses/{COURSE}/submissions?unit=u1&page=2"


@pytest.fixture
def prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ROOT_PREFIX, "/console")


def test_an_expired_page_goes_to_the_login_screen_under_the_prefix(
    world: World, prefixed: None
) -> None:
    response = world.client.get(LIST, headers=PAGE, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/console/login?next={quote(LIST, safe='')}"


def test_a_request_that_is_not_a_page_still_gets_a_401(world: World) -> None:
    """帯や一覧の自動更新（`live.js`）にログイン画面を差し込ませない。"""
    response = world.client.get(LIST, follow_redirects=False)

    assert response.status_code == 401


def test_an_expired_form_returns_to_the_page_it_was_sent_from(world: World, prefixed: None) -> None:
    response = world.client.post(
        "/review/sub_x/finalize",
        data={"justification": "x"},
        headers={**PAGE, "referer": "https://judge.example/console/review/sub_x"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/console/login?next=%2Freview%2Fsub_x"


def test_a_local_login_returns_to_the_page_under_the_prefix(world: World, prefixed: None) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)

    form = world.client.get("/auth/local", params={"next": LIST}).text
    assert 'name="next" value="/courses/' in form

    response = world.client.post(
        "/auth/local",
        data={"login": "instructor", "password": PASSWORD, "next": LIST},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/console{LIST}"


def test_a_login_never_sends_anyone_off_site(world: World) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)

    response = world.client.post(
        "/auth/local",
        data={"login": "instructor", "password": PASSWORD, "next": "https://evil.example/"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_a_google_login_returns_to_the_page(
    world: World, prefixed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aijudge_reviewconsole.app as review_app

    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))
    with world.database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(_a_google_settings())
        uow.commit()
    identity = GoogleOidcIdentity(sub="sub-1", email="taro@example.ac.jp", hd="example.ac.jp")
    monkeypatch.setattr(
        review_app, "GoogleOidcProvider", lambda: _StubOidcProvider(identity=identity)
    )

    started = world.client.get("/auth/login", params={"next": LIST}, follow_redirects=False)
    world.client.cookies.set("aijudge_oidc_state", started.cookies["aijudge_oidc_state"])
    world.client.cookies.set(NEXT_COOKIE, started.cookies[NEXT_COOKIE])

    callback = world.client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": "stub-state"},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert SESSION_COOKIE in callback.cookies
    assert callback.headers["location"] == f"/console{LIST}"
