"""状態を変える要求は同じオリジンからだけ受け付ける（#413）。

`SameSite=Lax` は同じサイトの別ホスト（`*.ryukoku.ac.jp` のほかのサーバ）から
の POST を止めない。`Origin`（無ければ `Sec-Fetch-Site`）で判断する。
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from aijudge_identity.origin_check import SameOriginMiddleware, same_origin

HOST = "judge.math.ryukoku.ac.jp"


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"host": HOST, "origin": f"https://{HOST}"}, True),
        ({"host": HOST, "origin": f"https://{HOST.upper()}:443"}, True),
        ({"host": HOST, "origin": "https://other.ryukoku.ac.jp"}, False),
        ({"host": HOST, "origin": "https://evil.example"}, False),
        ({"host": HOST, "origin": "null"}, False),
        ({"host": HOST, "sec-fetch-site": "same-origin"}, True),
        ({"host": HOST, "sec-fetch-site": "none"}, True),
        ({"host": HOST, "sec-fetch-site": "same-site"}, False),
        ({"host": HOST, "sec-fetch-site": "cross-site"}, False),
        # ブラウザ以外（Cookie を持たない）: CSRF の経路にならないので通す。
        ({"host": HOST}, True),
    ],
)
def test_the_origin_decides(headers: dict[str, str], allowed: bool) -> None:
    assert same_origin(headers) is allowed


def _client() -> TestClient:
    async def ok(request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/do", ok, methods=["GET", "POST"])])
    app.add_middleware(SameOriginMiddleware)
    return TestClient(app, base_url=f"https://{HOST}")


def test_a_post_from_another_host_of_the_same_site_is_refused() -> None:
    response = _client().post("/do", headers={"Origin": "https://other.ryukoku.ac.jp"})
    assert response.status_code == 403


def test_a_post_from_the_page_itself_goes_through() -> None:
    response = _client().post("/do", headers={"Origin": f"https://{HOST}"})
    assert response.status_code == 200


def test_a_get_is_never_checked() -> None:
    response = _client().get("/do", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
