"""エディタの画面のファイル欄（画像・PDF）を実ブラウザで動かす（2026-11-15）。

固定したいのは 4 つ。

まとめて選べる   1 回の選択で複数が一覧に並ぶ（`multiple`）
1 つずつ外せる   「外す」で 1 つだけ消え、全部外すと「提出」が押せなくなる
同じものは足さない  名前と大きさが同じなら足さず、理由を知らせる
例外を出さない   ページのエラーが無い

画面の HTML は実際のアプリが返したもの、スクリプトと CSS は配布する実物を使う。Monaco の
読み込みは要らない（ファイル欄は課題がコードを書かない形式のとき単独で動く）。

Playwright とブラウザが無い環境では skip する（`AIJUDGE_REQUIRE_BROWSER=1` なら失敗）。
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest
from test_ide_routes import _course_id, _editor, _learner, _second_task
from test_studentweb import World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_webui import ASSETS_DIR, STATIC_MOUNT

REQUIRED = os.environ.get("AIJUDGE_REQUIRE_BROWSER") == "1"


def _unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(f"AIJUDGE_REQUIRE_BROWSER=1 but {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


try:
    import playwright.sync_api as playwright_sync
except ImportError as error:
    _unavailable(f"could not import playwright.sync_api: {error}")

ORIGIN = "http://example.test"
TYPES = {".js": "text/javascript", ".css": "text/css"}


@pytest.fixture
def page(world: World):
    _editor(world)
    _second_task(world, (".png", ".pdf"))
    _learner(world)
    html = world.client.get(f"/courses/{_course_id(world)}/ide").text

    def serve(route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        if path.startswith(STATIC_MOUNT + "/"):
            asset = ASSETS_DIR / path.removeprefix(STATIC_MOUNT + "/")
            if asset.is_file():
                kind = TYPES.get(asset.suffix, "application/octet-stream")
                route.fulfill(body=asset.read_bytes(), content_type=kind)
                return
        if path == "/ide":
            route.fulfill(body=html, content_type="text/html; charset=utf-8")
            return
        # 記録の送信などは受けるだけでよい。
        route.fulfill(status=200, body="{}", content_type="application/json")

    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # ブラウザが無いだけ
            _unavailable(f"chromium is not installed: {error}")
        tab = browser.new_page()
        errors: list[str] = []
        tab.on("pageerror", lambda e: errors.append(str(e)))
        tab.route("**/*", serve)
        tab.goto(f"{ORIGIN}/ide")
        yield tab
        assert errors == [], errors
        browser.close()


def _files(tmp_path, count: int) -> list[str]:
    paths = []
    for n in range(count):
        path = tmp_path / f"cert{n}.png"
        path.write_bytes(b"\x89PNG" + bytes([n]) * 10)
        paths.append(str(path))
    return paths


def _names(page) -> list[str]:
    return page.eval_on_selector_all(
        ".ide-attach-list .file-name", "els => els.map(e => e.textContent.split('（')[0])"
    )


def test_several_files_are_listed_and_one_can_be_removed(page, tmp_path) -> None:
    page.set_input_files(".ide-attach-input", _files(tmp_path, 3))
    assert _names(page) == ["cert0.png", "cert1.png", "cert2.png"]

    page.click(".ide-attach-list li:nth-child(2) .file-remove")

    assert _names(page) == ["cert0.png", "cert2.png"]
    assert page.eval_on_selector(".ide-attach-submit", "e => e.disabled") is False


def test_the_same_file_is_not_added_twice(page, tmp_path) -> None:
    files = _files(tmp_path, 2)
    page.set_input_files(".ide-attach-input", files)

    page.set_input_files(".ide-attach-input", [files[0]])

    assert _names(page) == ["cert0.png", "cert1.png"]
    assert "同じファイルは追加済み" in page.text_content("[data-attach-notice]")


def test_removing_everything_disables_the_submit_button(page, tmp_path) -> None:
    page.set_input_files(".ide-attach-input", _files(tmp_path, 2))

    page.click(".ide-attach-clear")

    assert _names(page) == []
    assert page.eval_on_selector(".ide-attach-submit", "e => e.disabled") is True
