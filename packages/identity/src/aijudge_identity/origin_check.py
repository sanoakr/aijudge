"""状態を変える要求を、同じオリジンから来たものに限る（#413）。

セッション Cookie は `SameSite=Lax` だけで守っていた。Lax が防ぐのは
**クロスサイト**の POST で、**同じサイトの別ホスト**（`*.ryukoku.ac.jp` の
ほかのサーバ）に置かれたページや、そこの XSS からの POST は Cookie 付きで
通る ── 成績の確定・受講の変更・課題の削除が、利用者の知らないうちに
実行できた。`/auth/local` ではログイン CSRF（攻撃者のアカウントで
ログインさせる）も成立した。

ここでは **`Origin` を自分のホストと照合する**。フォームの POST にも
`fetch` にも、今のブラウザは必ず `Origin` を付ける。付いていない古い要求は
`Sec-Fetch-Site` で判断し、どちらも無い要求（curl・API トークンで叩く
スクリプト）は通す ── それらはブラウザの Cookie を持たないので CSRF の
経路にならない。

トークン方式にしないのは、全フォーム（100 を超える）とテストを書き換える
割に、守れる範囲が変わらないからである。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: 検査する（状態を変えうる）メソッド。
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: `Sec-Fetch-Site` のうち通す値。`none` は利用者自身の操作（アドレス欄など）。
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _host_only(value: str) -> str:
    """`host[:port]` からホスト名だけを取る（大文字小文字は揃える）。

    ポートは見ない。逆プロキシは `Host` をホスト名だけで渡す（`$host`）ので、
    ポートを比べると正規の要求まで落ちる。同じホストの別ポートで別の Web
    サーバを動かしていない前提である（運用機では nginx だけ）。
    """
    return urlsplit(f"//{value}").hostname or ""


def same_origin(headers: dict[str, str]) -> bool:
    """この要求を通してよいか。`headers` の名前は小文字。"""
    host = _host_only(headers.get("host", ""))
    origin = headers.get("origin")
    if origin is not None:
        if origin == "null":
            # サンドボックス化された iframe・data: URL から。正規の画面には無い。
            return False
        return bool(host) and urlsplit(origin).hostname == host
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in _ALLOWED_FETCH_SITES
    # どちらも無い: ブラウザ以外（Cookie を持たない）。CSRF の経路にならない。
    return True


class SameOriginMiddleware:
    """状態を変える要求のうち、別オリジンから来たものを 403 で断る。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET") not in UNSAFE_METHODS:
            await self._app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        if same_origin(headers):
            await self._app(scope, receive, send)
            return
        body = json.dumps(
            {"detail": "別のサイトからの操作は受け付けません（ページを開き直してください）"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["UNSAFE_METHODS", "SameOriginMiddleware", "same_origin"]
