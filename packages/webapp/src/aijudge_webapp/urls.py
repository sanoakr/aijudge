"""相手側アプリへの URL。"""

from __future__ import annotations

import re

from fastapi import Request

# ホスト名として通す形（#116）。**ヘッダの中身を信用しない。**
_HOSTNAME = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def counterpart_url(request: Request, *, configured: str, port: int) -> str:
    """相手側アプリの場所（#114）。

    **ブラウザが今いるホスト名をそのまま使う。** セッション Cookie は
    ホスト単位（`Domain` を付けていない・ポートは無視される）なので、
    起動時に決め打ちした名前へ渡すと、その名前で開いていない人の Cookie は
    付いていかない ── 1 台が `localhost`・IP・短い名前・FQDN・tailnet 名の
    どれでも応じる以上、「どの名前で来たか」は起動時には決まらない。

    `configured` が入っていればそちらを優先する。逆プロキシの後ろや、
    本当に別のホストに置いてある運用では、名前を知っているのは運用者の
    ほうだから（その場合セッションは共有されない ── 別のホストなら Cookie は
    そもそも届かない）。

    **ヘッダは検査してから使う**（#116）。`Host` も `X-Forwarded-*` も
    クライアントが決められるので、素通しすると 2 つ通る:

    - `X-Forwarded-Proto: javascript` と `%0a` を含むホスト名で
      `javascript://x%0aalert(1)/…` が作れる（改行が `//` のコメントを終わらせる）
    - リンク先が攻撃者のホストになり、同じ見た目のログイン画面に渡せる

    いま被害者に踏ませるのは難しい（ブラウザは自分が開いた URL の `Host` しか
    送らない）。**難しいことと塞がっていることは別である** ── 共有キャッシュや、
    外部入力を `X-Forwarded-*` に写す逆プロキシがあれば成立し、逆プロキシは
    #103 の次の段でまさに前に立てるものである。
    """
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if scheme not in ("http", "https"):
        # 知らないスキームは使わない（`javascript:` を href に置かせない）。
        scheme = "https" if request.url.scheme == "https" else "http"
    forwarded = request.headers.get("x-forwarded-host")
    host = (forwarded or request.url.hostname or "localhost").split(":")[0]
    if not _HOSTNAME.match(host):
        # 形の合わない名前は、そもそも自分のものではない。
        host = request.url.hostname or "localhost"
        if not _HOSTNAME.match(host):
            host = "localhost"
    return f"{scheme}://{host}:{port}"
