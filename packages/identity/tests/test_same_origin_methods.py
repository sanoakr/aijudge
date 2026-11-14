"""別オリジンの要求を断るのは POST だけではない（#413・#688）。

`test_same_origin_only.py` のテスト用経路は GET と POST しか持たないので、
`UNSAFE_METHODS` を `{"POST"}` に縮めても気づけなかった。PUT・PATCH・DELETE の
経路は実在する（コンソールの API・学習者側の削除など）。
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from aijudge_identity.origin_check import SameOriginMiddleware

HOST = "judge.math.ryukoku.ac.jp"

#: **`UNSAFE_METHODS` から作らない。** 本体の集合を縮める変更をすると、
#: そこから作った引数も一緒に縮んでテストが黙って減るだけになる。
STATE_CHANGING_METHODS = ("DELETE", "PATCH", "POST", "PUT")


def _client() -> TestClient:
    async def ok(request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/do", ok, methods=["GET", *STATE_CHANGING_METHODS])])
    app.add_middleware(SameOriginMiddleware)
    return TestClient(app, base_url=f"https://{HOST}")


@pytest.mark.parametrize("method", STATE_CHANGING_METHODS)
def test_a_state_changing_request_from_another_host_is_refused(method: str) -> None:
    response = _client().request(method, "/do", headers={"Origin": "https://other.ryukoku.ac.jp"})
    assert response.status_code == 403


@pytest.mark.parametrize("method", STATE_CHANGING_METHODS)
def test_a_state_changing_request_from_the_page_itself_goes_through(method: str) -> None:
    """対照。403 が経路の側（メソッドを許していない等）から来ていないこと。"""
    response = _client().request(method, "/do", headers={"Origin": f"https://{HOST}"})
    assert response.status_code == 200
