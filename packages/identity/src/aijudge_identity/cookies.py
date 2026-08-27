"""セッション Cookie の属性。

`Secure` を付けるかどうかは**配置**で決まる（TLS 終端が前に居るか）ので、
コードに固定できない。かといって既定を偽のままにすると、HTTPS で配置した
運用でも平文の経路が残る。

判断を 1 か所に集める。アプリが 2 つあり、片方で属性が抜けると
そちらだけセッションが盗める。

決め方は 2 つ。

- 明示（`AIJUDGE_SECURE_COOKIES=1`）。運用者が配置を知っている場合。
- `X-Forwarded-Proto: https`。リバースプロキシ（`tailscale serve`、nginx 等）
  が付ける。

後者を信用してよいのは、**この判断が属性を強める方向にしか働かない**から。
偽装されても `Secure` が付くだけで、平文で送られるようにはならない
（逆向きに使う判断 ── 「HTTPS だから認証を省く」等 ── には絶対に使えない）。
"""

from __future__ import annotations

import os

ENV_SECURE_COOKIES = "AIJUDGE_SECURE_COOKIES"


def secure_cookies(*, forwarded_proto: str | None = None) -> bool:
    """このリクエストで `Secure` を付けるか。"""
    explicit = os.environ.get(ENV_SECURE_COOKIES, "").strip().lower()
    if explicit in ("1", "true", "yes"):
        return True
    if explicit in ("0", "false", "no"):
        return False
    return (forwarded_proto or "").split(",")[0].strip().lower() == "https"


def session_cookie_kwargs(*, forwarded_proto: str | None = None) -> dict[str, object]:
    """`set_cookie` に渡す属性。

    `httponly` は JavaScript から読めなくする（XSS でセッションを盗ませない）。
    `samesite="lax"` は他サイトからの POST でセッションを使わせない。
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure_cookies(forwarded_proto=forwarded_proto),
        "path": "/",
    }
