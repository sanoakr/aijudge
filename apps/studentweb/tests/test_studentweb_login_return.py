"""セッションが切れたら、ログインし直して元のページへ戻る（学習者側）。

以前は `{"detail":"ログインしてください"}` の JSON がそのまま画面に出て、
ログイン後は一覧から開き直すしかなかった。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from test_studentweb import World, _a_google_settings, _StubOidcProvider
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_identity import GoogleOidcIdentity
from aijudge_persistence import ENV_OIDC_SECRET_KEY
from aijudge_studentweb import SESSION_COOKIE
from aijudge_webapp import NEXT_COOKIE

PAGE = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _task_path(world: World) -> str:
    return f"/tasks/{world.task_version.id}"


def test_an_expired_page_goes_to_the_login_screen_with_the_way_back(world: World) -> None:
    path = _task_path(world)

    response = world.client.get(path, headers=PAGE, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(path, safe='')}"


def test_a_request_that_is_not_a_page_still_gets_a_401(world: World) -> None:
    """自動更新や API の呼び出しにログイン画面の HTML を返さない。"""
    response = world.client.get(_task_path(world), follow_redirects=False)

    assert response.status_code == 401
    assert response.json() == {"detail": "ログインしてください"}


def test_an_expired_form_returns_to_the_page_it_was_sent_from(world: World) -> None:
    path = _task_path(world)

    response = world.client.post(
        f"{path}/submit",
        files={"upload": ("main.c", b"int main(void){return 0;}", "text/plain")},
        headers={**PAGE, "referer": f"http://testserver{path}"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(path, safe='')}"


def test_a_local_login_returns_to_the_page(world: World) -> None:
    world.register("s2400001")
    path = _task_path(world)

    form = world.client.get("/auth/local", params={"next": path}).text
    assert f'name="next" value="{path}"' in form

    response = world.client.post(
        "/auth/local",
        data={"login": "s2400001", "password": "correct horse battery", "next": path},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == path


def test_a_login_never_sends_anyone_off_site(world: World) -> None:
    world.register("s2400001")

    response = world.client.post(
        "/auth/local",
        data={
            "login": "s2400001",
            "password": "correct horse battery",
            "next": "//evil.example/",
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_a_failed_local_login_keeps_the_way_back(world: World) -> None:
    world.register("s2400001")
    path = _task_path(world)

    response = world.client.post(
        "/auth/local", data={"login": "s2400001", "password": "wrong", "next": path}
    )

    assert response.status_code == 401
    assert f'name="next" value="{path}"' in response.text


def test_the_way_back_is_escaped_on_the_login_screen(world: World) -> None:
    """戻り先は URL から来る。属性の外へ出させない。"""
    body = world.client.get("/auth/local", params={"next": '/x"><b>'}).text
    assert '/x"><b>' not in body
    assert 'value="/x&#34;&gt;&lt;b&gt;"' in body


def test_a_google_login_returns_to_the_page(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import aijudge_studentweb.app as student_app

    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))
    with world.database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(_a_google_settings())
        uow.commit()
    identity = GoogleOidcIdentity(sub="sub-1", email="taro@example.ac.jp", hd="example.ac.jp")
    monkeypatch.setattr(
        student_app, "GoogleOidcProvider", lambda: _StubOidcProvider(identity=identity)
    )
    path = _task_path(world)

    login_page = world.client.get("/login", params={"next": path}).text
    assert f'name="next" value="{path}"' in login_page

    started = world.client.get("/auth/login", params={"next": path}, follow_redirects=False)
    assert NEXT_COOKIE in started.cookies
    world.client.cookies.set("aijudge_oidc_state", started.cookies["aijudge_oidc_state"])
    world.client.cookies.set(NEXT_COOKIE, started.cookies[NEXT_COOKIE])

    callback = world.client.get(
        "/auth/callback",
        params={"code": "auth-code", "state": "stub-state"},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert SESSION_COOKIE in callback.cookies
    assert callback.headers["location"] == path
    # 預かりは使い切る。次のログインで昔のページへ飛ばさない。
    assert NEXT_COOKIE in callback.headers.get("set-cookie", "")
    assert 'aijudge_login_next=""' in callback.headers.get("set-cookie", "")


def test_a_google_login_without_a_destination_forgets_an_old_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中でやめた往復の預かりを、次の（一覧から入った）ログインに持ち越さない。"""
    import aijudge_studentweb.app as student_app

    monkeypatch.setenv(ENV_OIDC_SECRET_KEY, Fernet.generate_key().decode("ascii"))
    with world.database.unit_of_work() as uow:
        uow.identity.save_oidc_settings(_a_google_settings())
        uow.commit()
    monkeypatch.setattr(student_app, "GoogleOidcProvider", lambda: _StubOidcProvider())

    started = world.client.get("/auth/login", follow_redirects=False)

    assert 'aijudge_login_next=""' in started.headers.get("set-cookie", "")
