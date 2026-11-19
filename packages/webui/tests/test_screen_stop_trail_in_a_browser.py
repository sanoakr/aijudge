"""`screen.js` が、共有が止まった理由の手がかりを残すことを実ブラウザで確かめる（#783）。

試験中の画面の静止画は、映像トラックの `ended` でだけ止まる（ページ側から止める経路は無い）。
受験者から「タブを切り替えると止まる」と報告されたが、どの環境で・なぜ `ended` が来るかは
分かっていない。次に起きたとき理由が分かるよう、止まる前後の出来事（mute・unmute・表示状態の
変化・トラックの状態）を `onState("stopped", { reason, trail })` で渡す。

固定したいのは 3 つ。

ended で止まる           `stopped` が 1 回だけ出て、理由は `ended`
手がかりが付く           trail に出来事が時刻順で残り、各項目に表示状態とトラックの状態がある
上限を守る               trail は 8 件まで（古いものから落とす）

共有そのものは本物の画面を使わず、canvas の映像トラックを「画面全体」の共有に見せかける。

Playwright とブラウザが無い環境では skip する。skip は「未確認」で、「安全」ではない。
CI は `AIJUDGE_REQUIRE_BROWSER=1` で流し、skip ではなく失敗にする（#689）。
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

from aijudge_webui import ASSETS_DIR

REQUIRED = os.environ.get("AIJUDGE_REQUIRE_BROWSER") == "1"


def _unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(f"AIJUDGE_REQUIRE_BROWSER=1 but {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


try:
    import playwright.sync_api as playwright_sync
except ImportError as error:
    _unavailable(f"could not import playwright.sync_api: {error}")

# 本物の画面共有の代わりに、canvas の映像トラックを「画面全体」として返す。
FAKE_SHARE = """
window.__track = null;
navigator.mediaDevices.getDisplayMedia = function () {
  var canvas = document.createElement("canvas");
  canvas.width = 64; canvas.height = 36;
  canvas.getContext("2d").fillRect(0, 0, 64, 36);
  var stream = canvas.captureStream(5);
  var track = stream.getVideoTracks()[0];
  track.getSettings = function () { return { displaySurface: "monitor" }; };
  window.__track = track;
  return Promise.resolve(stream);
};
"""

START = """
window.__states = [];
AijudgeScreen.start({
  onFrame: function () {},
  onState: function (name, detail) { window.__states.push([name, detail || {}]); },
});
"""


@pytest.fixture
def page():
    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # ブラウザが入っていない
            _unavailable(f"chromium を起動できない: {error}")
        page = browser.new_page()
        page.errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.errors.append(str(e)))  # type: ignore[attr-defined]
        page.add_init_script(FAKE_SHARE)
        # 画面共有の API は安全な文脈（https か localhost）でしか使えない。about:blank では
        # `navigator.mediaDevices` が無いので、localhost の応答を差し替えて開く。
        page.route(
            "http://localhost/**",
            lambda route: route.fulfill(
                body="<!doctype html><html><body>x</body></html>", content_type="text/html"
            ),
        )
        page.goto("http://localhost/")
        page.add_script_tag(path=str(ASSETS_DIR / "screen.js"))
        page.evaluate(START)
        page.wait_for_function("window.__states.some(s => s[0] === 'sharing')")
        yield page
        browser.close()


def _stopped(page) -> list:
    return [s for s in page.evaluate("window.__states") if s[0] == "stopped"]


def test_it_stops_once_with_the_reason_ended(page) -> None:
    page.evaluate("window.__track.dispatchEvent(new Event('ended'))")
    page.wait_for_function("window.__states.some(s => s[0] === 'stopped')")
    # 2 回目の `ended` は無視される（二重に知らせない）。
    page.evaluate("window.__track.dispatchEvent(new Event('ended'))")

    stopped = _stopped(page)
    assert len(stopped) == 1
    assert stopped[0][1]["reason"] == "ended"
    assert not page.errors  # type: ignore[attr-defined]


def test_the_trail_keeps_what_happened_before_it_stopped(page) -> None:
    page.evaluate("window.__track.dispatchEvent(new Event('mute'))")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.evaluate("window.__track.dispatchEvent(new Event('ended'))")
    page.wait_for_function("window.__states.some(s => s[0] === 'stopped')")

    trail = _stopped(page)[0][1]["trail"]
    assert [item["e"] for item in trail] == ["mute", "visibility", "ended"]
    times = [item["ms"] for item in trail]
    assert times == sorted(times), "時刻順でない"
    for item in trail:
        assert item["v"] in ("visible", "hidden"), "表示状態が無い"
        assert item["rs"] in ("live", "ended"), "トラックの状態が無い"
        assert isinstance(item["muted"], bool)


def test_the_trail_is_capped_at_eight(page) -> None:
    for _ in range(20):
        page.evaluate("window.__track.dispatchEvent(new Event('mute'))")
    page.evaluate("window.__track.dispatchEvent(new Event('ended'))")
    page.wait_for_function("window.__states.some(s => s[0] === 'stopped')")

    trail = _stopped(page)[0][1]["trail"]
    assert len(trail) == 8
    assert trail[-1]["e"] == "ended", "最後は止まった理由"
