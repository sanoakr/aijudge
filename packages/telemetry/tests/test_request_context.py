"""リクエストに 1 本の ID が通ること。

これが無かったので、web の提出とワーカーの失敗を突き合わせられなかった
（#60 / #80）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging

from aijudge_telemetry import RequestContextMiddleware, configure_logging, current_context


def _scope(path: str = "/tasks/1", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
    }


async def _drive(app, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _app(status: int = 200, on_call=None):
    async def inner(scope, receive, send) -> None:
        if on_call is not None:
            on_call()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return inner


def test_one_line_per_request_carrying_the_id() -> None:
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    sent = asyncio.run(_drive(RequestContextMiddleware(_app()), _scope()))

    event = json.loads(stream.getvalue())
    assert event["logger"] == "aijudge.access"
    assert event["status"] == 200
    assert event["path"] == "/tasks/1"
    assert event["method"] == "GET"
    assert event["request_id"]
    # 応答にも返す。学生の問い合わせに ID が付いていれば 1 件を引ける。
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"x-request-id", event["request_id"].encode()) in start["headers"]


def test_the_id_is_visible_to_the_handler() -> None:
    """ハンドラ内のログにも同じ ID が載る（文脈を経由するので extra は要らない）。"""
    seen: dict[str, object] = {}
    configure_logging("learner-web", fmt="json", stream=io.StringIO())

    asyncio.run(
        _drive(
            RequestContextMiddleware(_app(on_call=lambda: seen.update(current_context()))), _scope()
        )
    )

    assert "request_id" in seen
    # 抜けたら元に戻る。
    assert current_context() == {}


def test_an_id_from_the_proxy_is_kept() -> None:
    """前段が付けた ID を引き継ぐ。振り直すとプロキシのログと鍵が別になる。"""
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    asyncio.run(
        _drive(RequestContextMiddleware(_app()), _scope(headers=[(b"X-Request-ID", b"from-nginx")]))
    )

    assert json.loads(stream.getvalue())["request_id"] == "from-nginx"


def test_an_absurd_id_from_outside_is_not_trusted() -> None:
    """外から来た値でログ 1 行を任意長にできてはいけない。"""
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    asyncio.run(
        _drive(RequestContextMiddleware(_app()), _scope(headers=[(b"x-request-id", b"x" * 500)]))
    )

    assert json.loads(stream.getvalue())["request_id"] != "x" * 500


def test_the_query_string_never_reaches_the_log() -> None:
    """`?token=` の類を平文で残さない（P7）。"""
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    asyncio.run(_drive(RequestContextMiddleware(_app()), _scope(path="/enter?token=secret")))

    event = json.loads(stream.getvalue())
    assert event["path"] == "/enter"
    assert "secret" not in stream.getvalue()


def test_a_failing_handler_still_leaves_a_line() -> None:
    """500 の要求こそ記録が要る。"""
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    async def broken(scope, receive, send) -> None:
        raise RuntimeError("boom")

    with contextlib.suppress(RuntimeError):
        asyncio.run(_drive(RequestContextMiddleware(broken), _scope()))

    event = json.loads(stream.getvalue())
    assert event["status"] == 0  # 応答が始まる前に落ちた


def test_quiet_paths_are_not_logged() -> None:
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    app = RequestContextMiddleware(_app(), quiet_paths=("/health",))
    asyncio.run(_drive(app, _scope(path="/health")))

    assert stream.getvalue() == ""


def test_the_polling_endpoint_is_quiet() -> None:
    """締切前は 1 人あたり毎分 30 行になる。読むべき行が埋まる。"""
    stream = io.StringIO()
    configure_logging("learner-web", fmt="json", stream=stream)

    app = RequestContextMiddleware(_app(), quiet_suffixes=("/state",))
    asyncio.run(_drive(app, _scope(path="/submissions/s-1/state")))
    assert stream.getvalue() == ""

    # 提出そのものの閲覧は残る。
    asyncio.run(_drive(app, _scope(path="/submissions/s-1")))
    assert json.loads(stream.getvalue())["path"] == "/submissions/s-1"


def test_websockets_pass_through() -> None:
    configure_logging("learner-web", fmt="json", stream=io.StringIO())
    calls: list[str] = []

    async def inner(scope, receive, send) -> None:
        calls.append(scope["type"])

    asyncio.run(_drive(RequestContextMiddleware(inner), {"type": "websocket"}))
    assert calls == ["websocket"]


def teardown_module() -> None:
    logging.getLogger().handlers.clear()
