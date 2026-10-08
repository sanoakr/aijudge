"""画面の自動更新の取得を見分ける（`static/live.js`・2026-10-08）。

教員画面の残数や一覧は、`live.js` が一定間隔で同じ URL を取り直して差し替える。
その取得には `X-Aijudge-Live: 1` を付けてあり、**サーバはこれを見て、一度だけ出す
知らせ（`Notices.take`）を消さない**。消すと、操作の結果（確定した件数など）が、
教員が戻った画面に出る前に、裏の取得に取られる。

ASGI の素の形にしてあるのは、要求の処理が走る間だけ値を立て、スレッドで走る
ルート（FastAPI の同期関数）にも `ContextVar` が引き継がれるようにするため。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from . import notices

LIVE_HEADER = b"x-aijudge-live"


class LivePollMiddleware:
    """`X-Aijudge-Live` 付きの要求の間、`notices.live_poll` を立てる。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_live(scope):
            await self._app(scope, receive, send)
            return
        token = notices.live_poll.set(True)
        try:
            await self._app(scope, receive, send)
        finally:
            notices.live_poll.reset(token)


def _is_live(scope: Scope) -> bool:
    return any(name == LIVE_HEADER and value == b"1" for name, value in scope.get("headers", []))


__all__ = ["LIVE_HEADER", "LivePollMiddleware"]
