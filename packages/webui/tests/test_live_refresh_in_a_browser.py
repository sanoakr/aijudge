"""`live.js` を実ブラウザで動かす（2026-10-08）。

固定したいのは 6 つ。

残数が自動で変わる        `data-live` の区画が、開き直さずに差し替わる
行を足す・直す・消す      `data-live-rows` の行が鍵で突き合わされる
入力中は触らない          書きかけの欄がある区画は差し替えない（フォーカスだけでも）
取得に印が付く            `X-Aijudge-Live: 1`（サーバは知らせを消さない）
ログイン切れで止まる      止まって、開き直しを促す
例外を出さない            ページのエラーが無い

Playwright とブラウザが無い環境（`uv run --group guide-shots playwright install chromium`
をしていない）では skip する。skip は「未確認」で、「安全」ではない。
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator

import pytest

from aijudge_webui import ASSETS_DIR

playwright_sync = pytest.importorskip("playwright.sync_api")


def _rail(n: int) -> str:
    return (
        '<aside class="rail" data-refresh-url="/rail-fragment" data-course-id="crs_1">'
        f'<nav><a class="nav" href="/f"><span class="t">確定処理</span>'
        f'<span class="c attn" id="railcount">{5 + n}</span></a></nav></aside>'
    )


def _page(n: int) -> str:
    rows = "".join(
        f'<tr data-live-key="r{i}"><td>row {i} v{n if i == 2 else 0}</td>'
        f'<td><a href="/r/{i}">open</a></td></tr>'
        for i in range(1, 3 + (1 if n >= 2 else 0))
        if not (n >= 3 and i == 1)
    )
    attn = " attn" if n >= 1 else ""
    return f"""<!doctype html><html><body><main>
{_rail(n)}
<span id="online-users" data-url="/online-users" title="">オンライン <b>1</b> 人</span>
<span id="count" data-live="count" class="pill{attn}">未確定 {10 - n}</span>
<table><tbody data-live="rows" data-live-rows>{rows}</tbody></table>
<div data-live="form"><form><textarea id="just" name="j"></textarea></form>
<span id="formcount">件数 {n}</span></div>
</main><script src="/live.js" data-interval="300" defer></script></body></html>"""


class _Site:
    def __init__(self) -> None:
        self.n = 0
        self.expired = False
        self.live_requests = 0
        self.rail_queries: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                body, ctype = outer.route(self.path, self.headers.get("X-Aijudge-Live"))
                if body is None:
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, path: str, live: str | None) -> tuple[bytes | None, str]:
        if path == "/live.js":
            return (ASSETS_DIR / "live.js").read_bytes(), "text/javascript"
        if path.startswith("/login"):
            return b"<html><body>login</body></html>", "text/html; charset=utf-8"
        if path.startswith("/rail-fragment"):
            if live == "1":
                self.live_requests += 1
            self.rail_queries.append(path)
            return _rail(self.n).encode(), "text/html; charset=utf-8"
        if path.startswith("/online-users"):
            if live == "1":
                self.live_requests += 1
            data = {"total": 1 + self.n, "staff": 1, "learners": self.n, "window_minutes": 5}
            return json.dumps(data).encode(), "application/json"
        if path.startswith("/bump"):
            self.n += 1
            return b"ok", "text/plain"
        if path.startswith("/expire"):
            self.expired = True
            return b"ok", "text/plain"
        if self.expired:
            return None, ""
        if live == "1":
            self.live_requests += 1
        return _page(self.n).encode(), "text/html; charset=utf-8"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def site() -> Iterator[_Site]:
    instance = _Site()
    yield instance
    instance.close()


@pytest.fixture
def page(site: _Site):
    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # ブラウザが入っていない
            pytest.skip(f"chromium を起動できない: {error}")
        page = browser.new_page()
        page.errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.errors.append(str(e)))  # type: ignore[attr-defined]
        page.goto(site.url + "/")
        yield page
        browser.close()


def _bump(site: _Site, times: int = 1) -> None:
    for _ in range(times):
        site.n += 1


def test_counts_rows_and_the_state_line_update_without_a_reload(site: _Site, page) -> None:
    assert page.inner_text("#count") == "未確定 10"
    _bump(site, 2)

    page.wait_for_function("document.querySelector('#count').textContent.includes('8')")
    assert "attn" in page.get_attribute("#count", "class"), "見た目の印（class）が更新されない"
    assert page.locator("tbody tr").count() == 3, "増えた行が足されていない"
    assert "v2" in page.inner_text("tr[data-live-key='r2']"), "変わった行が直っていない"
    assert site.live_requests > 0, "取得に X-Aijudge-Live が付いていない"
    assert "自動更新" in page.inner_text("#live-status")

    _bump(site)
    page.wait_for_function("document.querySelectorAll('tbody tr').length === 2")
    assert page.locator("tr[data-live-key='r1']").count() == 0, "無くなった行が残っている"
    assert not page.errors  # type: ignore[attr-defined]


def test_a_region_being_edited_is_left_alone(site: _Site, page) -> None:
    page.fill("#just", "書きかけの根拠")
    _bump(site, 2)
    page.wait_for_function("document.querySelector('#count').textContent.includes('8')")

    assert page.inner_text("#formcount") == "件数 0", "入力中の区画が差し替わった"
    assert page.input_value("#just") == "書きかけの根拠"

    # 空にしても、フォーカスが残る間は触らない。外せば更新される。
    page.fill("#just", "")
    page.wait_for_timeout(900)
    assert page.inner_text("#formcount") == "件数 0"
    page.evaluate("document.activeElement.blur()")
    page.wait_for_function("document.querySelector('#formcount').textContent.includes('2')")


def test_it_stops_and_says_so_when_the_login_expires(site: _Site, page) -> None:
    site.expired = True
    page.wait_for_function(
        "document.querySelector('#live-status')?.textContent.includes('有効期限')"
    )
    assert "自動更新を止めました" in page.inner_text("#live-status")


def test_the_online_count_in_the_bar_is_refreshed(site: _Site, page) -> None:
    """上部バーの人数は、区画の差し替えとは別の軽い経路で取り直す（どの画面でも動く）。"""
    assert page.inner_text("#online-users b") == "1"
    _bump(site, 3)

    page.wait_for_function("document.querySelector('#online-users b').textContent === '4'")

    assert "学習者 3" in page.get_attribute("#online-users", "title")


def test_the_rail_counts_are_refreshed_without_fetching_the_page(site: _Site, page) -> None:
    """帯の件数は、ページ全体ではなく帯だけを返す経路で取り直す（どの画面でも動く）。"""
    assert page.inner_text("#railcount") == "5"
    _bump(site, 2)

    page.wait_for_function("document.querySelector('#railcount').textContent === '7'")

    assert site.rail_queries, "帯の取り直しの経路を呼んでいない"
    assert "course_id=crs_1" in site.rail_queries[0], "どのコースの帯かを伝えていない"
