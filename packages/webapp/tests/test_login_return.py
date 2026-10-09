"""ログインし直した後の戻り先（`aijudge_webapp.login_return`）。

戻り先は URL と Cookie に載る外部入力なので、**アプリ内のパス以外を通さない**
ことをここで固定する。画面を通した確かめは各アプリのテストにある。
"""

from __future__ import annotations

import pytest
from fastapi import Request

from aijudge_webapp.login_return import login_location, return_path, safe_next, wants_page


def _request(
    method: str = "GET",
    *,
    path: str = "/",
    query: str = "",
    headers: dict[str, str] | None = None,
) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("judge.example", 8080),
            "path": path,
            "query_string": query.encode(),
            "headers": raw,
        }
    )


@pytest.mark.parametrize(
    "value",
    ["/tasks/tv_1", "/courses/c1/submissions?unit=u1&page=2", "/"],
)
def test_a_path_inside_the_app_is_kept(value: str) -> None:
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example/",
        "javascript:alert(1)",
        "tasks/tv_1",
        "/tasks\r\nSet-Cookie: x=1",
        "/" + "a" * 3000,
    ],
)
def test_anything_that_could_leave_the_app_is_dropped(value: str | None) -> None:
    """open redirect にさせない。別ホストへ読まれる形・ヘッダを割る形は落とす。"""
    assert safe_next(value) is None


@pytest.mark.parametrize("value", ["/login", "/login?next=/x", "/logout", "/auth/local"])
def test_the_login_pages_are_not_a_destination(value: str) -> None:
    """戻り先がログイン画面だと、ログインした直後にまたログイン画面が出る。"""
    assert safe_next(value) is None


def test_only_a_page_request_counts_as_a_page() -> None:
    """自動更新の `fetch` は `Accept: */*` で来る。ログイン画面を差し込ませない。"""
    assert wants_page(_request(headers={"accept": "text/html,application/xhtml+xml,*/*"}))
    assert not wants_page(_request(headers={"accept": "*/*"}))
    assert not wants_page(_request())


def test_a_get_returns_to_the_page_itself() -> None:
    request = _request(path="/courses/c1/submissions", query="unit=u1&page=2")
    assert return_path(request) == "/courses/c1/submissions?unit=u1&page=2"


def test_a_post_returns_to_the_page_it_was_sent_from() -> None:
    """送信先を GET で開き直すと 405 になる。送った元のページへ戻す。"""
    request = _request(
        "POST",
        path="/tasks/tv_1/submit",
        headers={"referer": "https://judge.example/tasks/tv_1?tab=code"},
    )
    assert return_path(request) == "/tasks/tv_1?tab=code"


def test_a_post_without_a_referer_has_nowhere_to_return() -> None:
    assert return_path(_request("POST", path="/tasks/tv_1/submit")) is None


def test_the_referer_loses_the_proxy_prefix() -> None:
    """`Referer` は外から見たパス。戻り先はアプリ内のパスで持つ（接頭辞は後で足す）。"""
    request = _request(
        "POST",
        path="/reviews/s1/finalize",
        headers={"referer": "https://judge.example/console/reviews/s1"},
    )
    assert return_path(request, prefix="/console") == "/reviews/s1"


def test_a_referer_outside_the_prefix_is_not_ours() -> None:
    """接頭辞の外は別のアプリ（学習者側）のページなので、戻り先にしない。"""
    request = _request(
        "POST",
        path="/reviews/s1/finalize",
        headers={"referer": "https://judge.example/consoles/x"},
    )
    assert return_path(request, prefix="/console") is None


def test_the_login_location_carries_the_destination_encoded() -> None:
    assert login_location(None) == "/login"
    assert login_location("/a?b=1&c=2") == "/login?next=%2Fa%3Fb%3D1%26c%3D2"
