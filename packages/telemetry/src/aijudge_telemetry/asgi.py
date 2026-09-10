"""リクエストに 1 本の ID を通す ASGI ミドルウェア。

**これが無かったので、web の提出とワーカーの失敗を突き合わせられなかった。**
#60 と #80 はどちらも「エラーはワーカーのログにしか出ない」まま、画面からは
「採点が遅い」としか見えなかった（`docs/RUNNING.md`）。原因はログが少ない
ことではなく、2 つのプロセスのログを繋ぐ鍵が無かったことである。

ここで発行した `request_id` を提出 → ジョブ → 採点実行へ引き継げば、
1 回の提出に関する行が全プロセスから拾える。

**アクセスログもここで出す。** uvicorn の既定書式はパスとクエリを一体で
持つので、`?token=` の類が平文で残る。経路だけを出す方に寄せ、uvicorn 側の
アクセスログは黙らせる（`uvicorn_log_config`）。

ASGI の生の形で書いてあるのは、このパッケージに依存を持たせないため
（starlette も fastapi も import しない）。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .context import bind
from .logging_setup import redact_query

HEADER = b"x-request-id"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _incoming_request_id(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """前段（nginx 等）が付けた ID があれば引き継ぐ。

    自前で振り直すと、プロキシのログとアプリのログが別の鍵になる。
    ただし外から来た値をそのまま使うので、長さで切る（ログ 1 行を
    任意長にできてはいけない）。
    """
    for name, value in headers:
        if name.lower() == HEADER:
            candidate = value.decode("latin-1", "replace").strip()
            if candidate and len(candidate) <= 64:
                return candidate
    return None


class RequestContextMiddleware:
    """`request_id` を発行して文脈に積み、1 リクエスト 1 行のアクセスログを出す。

    記録しない経路を 2 通りで指定できる。

    - `quiet_paths` — 前方一致（画像のような静的な取り出し）
    - `quiet_suffixes` — 後方一致（`/submissions/{id}/state` のような
      画面が数秒ごとに叩く問い合わせ。締切前は 1 人あたり毎分 30 行になり、
      **読むべき行が埋まる**）

    経路のどこに識別子が挟まるかは呼び出し側にしか分からないので、
    パターンではなく 2 つの単純な規則で表す。
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        quiet_paths: tuple[str, ...] = (),
        quiet_suffixes: tuple[str, ...] = (),
        logger_name: str = "aijudge.access",
    ) -> None:
        self.app = app
        self.quiet_paths = quiet_paths
        self.quiet_suffixes = quiet_suffixes
        self.logger = logging.getLogger(logger_name)

    def _is_quiet(self, path: str) -> bool:
        return path.startswith(self.quiet_paths) or path.endswith(self.quiet_suffixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        request_id = _incoming_request_id(headers) or uuid.uuid4().hex
        path = redact_query(scope.get("path", ""))
        method = scope.get("method", "")
        status = 0

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                # 応答にも載せる。学生の問い合わせに ID が付いていれば、
                # その 1 リクエストのログをすぐ引ける。
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (HEADER, request_id.encode("latin-1"))]
            await send(message)

        started = time.monotonic()
        with bind(request_id=request_id, method=method, path=path):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                if not self._is_quiet(path):
                    self.logger.info(
                        "%s %s %s",
                        method,
                        path,
                        status or "-",
                        extra={
                            "status": status,
                            "duration_ms": round((time.monotonic() - started) * 1000, 1),
                        },
                    )
