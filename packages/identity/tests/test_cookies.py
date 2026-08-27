"""セッション Cookie の属性を固定する。

アプリが 2 つあり、片方で属性が抜けるとそちらだけセッションが盗める。
判断は 1 か所に集めてある。
"""

from __future__ import annotations

import pytest

from aijudge_identity import ENV_SECURE_COOKIES, secure_cookies, session_cookie_kwargs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV_SECURE_COOKIES, raising=False)


def test_plain_http_does_not_get_a_secure_cookie() -> None:
    """既定は偽。真にすると localhost の平文アクセスでログインできない。"""
    assert secure_cookies() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
def test_the_environment_can_force_it(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ENV_SECURE_COOKIES, value)
    assert secure_cookies() is True


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_the_environment_can_forbid_it(monkeypatch, value: str) -> None:
    """プロキシが居ても運用者が明示的に切れること。"""
    monkeypatch.setenv(ENV_SECURE_COOKIES, value)
    assert secure_cookies(forwarded_proto="https") is False


def test_a_forwarded_https_proto_turns_it_on() -> None:
    """`tailscale serve` や nginx が前に居る配置。"""
    assert secure_cookies(forwarded_proto="https") is True


def test_a_proxy_chain_uses_the_first_hop() -> None:
    """`X-Forwarded-Proto: https, http` は最初が利用者側。"""
    assert secure_cookies(forwarded_proto="https, http") is True
    assert secure_cookies(forwarded_proto="http, https") is False


def test_a_missing_or_odd_header_stays_off() -> None:
    for value in (None, "", "HTTP", "ftp", "  "):
        assert secure_cookies(forwarded_proto=value) is False


def test_the_forged_header_can_only_tighten(monkeypatch) -> None:
    """偽装されても `Secure` が付くだけで、平文で送られるようにはならない。

    この判断を逆向きに使ってはならない（「HTTPS だから認証を省く」等）。
    """
    assert secure_cookies(forwarded_proto="https") is True
    # 偽装で「HTTPS でない」と言われても、明示設定が優先される。
    monkeypatch.setenv(ENV_SECURE_COOKIES, "1")
    assert secure_cookies(forwarded_proto="http") is True


def test_the_other_attributes_are_always_set() -> None:
    """`httponly` と `samesite` は配置に関係なく必要。"""
    kwargs = session_cookie_kwargs()
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["path"] == "/"
