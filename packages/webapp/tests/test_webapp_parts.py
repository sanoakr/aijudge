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
