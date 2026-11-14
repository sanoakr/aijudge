"""ID トークンの主張（iss・aud・exp）の検査と、空の期待値の扱いを固定する（#688）。

`test_oidc.py` のトークンは `iss`・`exp` が常に正しく、`aud` も既定値でしか
作っていなかった。そのため `JWTClaimsRegistry(...).validate(...)` を丸ごと外しても
全部通っていた。ここでは **正しい鍵（`_KEY`）で署名したまま** 主張を 1 つだけ
崩す ── 署名検証で先に落ちると、主張の検査が効いているかは分からない。
"""

from __future__ import annotations

import time

import pytest
from joserfc import jwt
from test_oidc import _KEY, CLIENT_ID, KID, a_provider, an_id_token, exchange

from aijudge_identity import AuthenticationFailed

#: 期限切れ・発行時刻に使う「十分に昔」。joserfc の既定の猶予（0 秒）より大きく取る。
_ONE_HOUR = 3600


def _a_token(**overrides: object) -> str:
    """`an_id_token` と同じ中身（正しい nonce・hd・確認済みメール）から、
    指定した主張だけを差し替える。値に `None` を渡すとその主張を消す。"""
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "sub-1",
        "email": "taro@example.ac.jp",
        "email_verified": True,
        "hd": "example.ac.jp",
        "nonce": "n",
        "iat": now,
        "exp": now + _ONE_HOUR,
    }
    for name, value in overrides.items():
        if value is None:
            del payload[name]
        else:
            payload[name] = value
    return jwt.encode({"alg": "RS256", "kid": KID}, payload, _KEY)


def test_the_unmodified_token_logs_in() -> None:
    """対照。下の各テストが落ちるのは、差し替えた主張のせいだけであること。"""
    identity = exchange(a_provider(id_token=_a_token()), nonce="n")
    assert identity.email == "taro@example.ac.jp"


@pytest.mark.parametrize("aud", ["another-client", CLIENT_ID[:-1]])
def test_a_token_issued_to_another_client_is_rejected(aud: str) -> None:
    """**`aud` が自分の `client_id` でないトークンは通さない。**

    別の OAuth クライアント（同じ Google を使う他のサービス）向けに発行された
    ID トークンを持ち込まれても、ここで止まらなければ本人として通ってしまう。

    `client_id` の一部だけの値も試す。`"value"` を `"values"` と書き違えると
    joserfc は文字列に対する `in` で比べるので、部分文字列が通るようになる。
    """
    provider = a_provider(id_token=an_id_token(nonce="n", aud=aud))
    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


def test_a_token_from_another_issuer_is_rejected() -> None:
    """**`iss` が Google でないトークンは通さない。**"""
    provider = a_provider(id_token=_a_token(iss="https://evil.example"))
    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


def test_an_expired_token_is_rejected() -> None:
    """**期限切れのトークンは通さない。** 盗まれた古いトークンを使い回させない。"""
    now = int(time.time())
    provider = a_provider(id_token=_a_token(iat=now - 2 * _ONE_HOUR, exp=now - _ONE_HOUR))
    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


@pytest.mark.parametrize("claim", ["iss", "aud", "exp"])
def test_a_token_missing_a_required_claim_is_rejected(claim: str) -> None:
    """**欠落も通さない**（3 つとも essential）。

    `exp` を essential から外すと、期限の無いトークンが通る ── 期限切れの
    検査は「あれば見る」だけなので、上のテストではこの変化に気づけない。
    """
    provider = a_provider(id_token=_a_token(**{claim: None}))
    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="n")


# --------------------------------------------------------------------------
# 空の期待値（#688）
#
# `exchange_code` は `secrets.compare_digest(expected_state, actual_state)` だけで
# state を見るので、空文字どうしは一致する。いまは呼び出し側（両アプリの
# `/auth/callback`）が state の Cookie の無い要求を先に断っているので届かないが、
# アダプタ自身は空の期待値を断らない。呼び出し側の番が外れたときの最後の砦が
# 無い状態なので、exchange_code が空の期待値を断る（下の 2 本）。
# --------------------------------------------------------------------------


def test_an_empty_expected_state_is_rejected() -> None:
    """nonce は正しいまま、state の期待値と実際の値を両方空にする。"""
    provider = a_provider(id_token=_a_token())
    with pytest.raises(AuthenticationFailed):
        exchange(provider, state="", actual_state="", nonce="n")


def test_an_empty_expected_nonce_is_rejected() -> None:
    """state は正しいまま、nonce の期待値とトークンの nonce を両方空にする。"""
    provider = a_provider(id_token=_a_token(nonce=""))
    with pytest.raises(AuthenticationFailed):
        exchange(provider, nonce="")
