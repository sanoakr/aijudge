"""2 つの Web アプリが共有する部品の振る舞いを固定する（段階 1-2、#437）。

以前は各アプリの `app.py` に 1 部ずつあり、どちらの写しも直接は試されて
いなかった（画面を通した間接の確認だけ）。1 か所に寄せたので、ここで直接見る。
"""

from __future__ import annotations

import io
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest
from fastapi import HTTPException, Request

import aijudge_webapp as webapp
from aijudge_core.ids import TenantId, UserId
from aijudge_identity import Principal

REPO_ROOT = Path(__file__).resolve().parents[3]
VIDEO_BYTES = b"0123456789"


def _request(headers: dict[str, str] | None = None, *, host: str = "judge.example") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    raw.append((b"host", host.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": (host, 8080),
            "path": "/",
            "query_string": b"",
            "headers": raw,
        }
    )


def test_the_version_is_the_root_projects() -> None:
    """各パッケージの `0.0.1`（プレースホルダ）ではなく、ルートの版を読む。"""
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert webapp.read_app_version() == root["project"]["version"]


def test_the_copyright_notice_follows_the_license() -> None:
    license_text = (REPO_ROOT / "LICENSE").read_text()
    match = re.search(r"^\s*Copyright\s+(\d{4})\s+(.+?)\s*$", license_text, re.MULTILINE)
    assert match is not None
    notice = webapp.read_copyright_notice()
    assert notice.startswith(f"© {match.group(1)}")
    assert notice.endswith(match.group(2))


def test_a_configured_counterpart_wins() -> None:
    request = _request({"x-forwarded-host": "elsewhere.example"})
    assert (
        webapp.counterpart_url(request, configured="https://learn.example", port=8080)
        == "https://learn.example"
    )


def test_the_counterpart_uses_the_host_the_browser_came_by() -> None:
    """Cookie はホスト単位なので、起動時の名前ではなく今の名前へ渡す（#114）。"""
    assert webapp.counterpart_url(_request(), configured="", port=8765) == (
        "http://judge.example:8765"
    )


def test_an_unknown_scheme_is_not_used() -> None:
    """`javascript:` を href に置かせない（#116）。"""
    request = _request({"x-forwarded-proto": "javascript"})
    assert webapp.counterpart_url(request, configured="", port=8765).startswith("http://")


def test_a_malformed_forwarded_host_is_not_used() -> None:
    """改行を含む名前は、そもそも自分のものではない（#116）。"""
    request = _request({"x-forwarded-host": "evil.example%0aalert(1)"})
    assert webapp.counterpart_url(request, configured="", port=8765) == (
        "http://judge.example:8765"
    )


@dataclass
class _Artifact:
    storage_key: str = "videos/a.mp4"
    filename: str = "a.mp4"


class _Store:
    def open_read(self, key: str) -> IO[bytes]:
        return io.BytesIO(VIDEO_BYTES)

    def size(self, key: str) -> int:
        return len(VIDEO_BYTES)


def test_no_video_store_reads_as_no_submission() -> None:
    with pytest.raises(HTTPException) as caught:
        webapp.serve_video(None, _request(), _Artifact(), "art_1")
    assert caught.value.status_code == 404


def test_a_whole_video_is_served_with_its_length() -> None:
    response = webapp.serve_video(_Store(), _request(), _Artifact(), "art_1")  # type: ignore[arg-type]
    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(VIDEO_BYTES))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_range_is_served_as_partial_content() -> None:
    request = _request({"range": "bytes=2-5"})
    response = webapp.serve_video(_Store(), request, _Artifact(), "art_1")  # type: ignore[arg-type]
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 2-5/{len(VIDEO_BYTES)}"
    assert response.headers["content-length"] == "4"


COOKIE = "aijudge_session"
ALICE = Principal(
    user_id=UserId("usr_" + "a" * 32),
    tenant_id=TenantId("ten_" + "0" * 32),
    login="s2400001",
    display_name="s2400001",
)


class _Resolver:
    """呼ばれた回数を数える `resolve`。"""

    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal
        self.tokens: list[str] = []

    def __call__(self, token: str) -> Principal | None:
        self.tokens.append(token)
        return self.principal


def test_the_principal_is_resolved_once_per_request() -> None:
    """帯（#189）の context processor と依存が同じ値を使う。引くのは 1 回。"""
    request = _request({"cookie": f"{COOKIE}=tok"})
    resolve = _Resolver(ALICE)
    first = webapp.current_principal(request, cookie=COOKIE, resolve=resolve)
    second = webapp.current_principal(request, cookie=COOKIE, resolve=resolve)
    assert first == second == ALICE
    assert resolve.tokens == ["tok"]
    assert getattr(request.state, webapp.PRINCIPAL_STATE) == ALICE


def test_an_invalid_session_is_remembered_as_none() -> None:
    """`None` も覚える ── 番兵で「まだ引いていない」と区別する。"""
    request = _request({"cookie": f"{COOKIE}=expired"})
    resolve = _Resolver(None)
    assert webapp.current_principal(request, cookie=COOKIE, resolve=resolve) is None
    assert webapp.current_principal(request, cookie=COOKIE, resolve=resolve) is None
    assert resolve.tokens == ["expired"]


def test_no_cookie_does_not_touch_the_store() -> None:
    resolve = _Resolver(ALICE)
    assert webapp.current_principal(_request(), cookie=COOKIE, resolve=resolve) is None
    assert resolve.tokens == []


def test_each_request_resolves_afresh() -> None:
    """キャッシュは要求をまたがない（ログアウトした次の要求は引き直す）。"""
    resolve = _Resolver(ALICE)
    for _ in range(2):
        webapp.current_principal(
            _request({"cookie": f"{COOKIE}=tok"}), cookie=COOKIE, resolve=resolve
        )
    assert resolve.tokens == ["tok", "tok"]
