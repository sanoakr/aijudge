"""課題の画面のファイル選びを実ブラウザで動かす（2026-11-15）。

固定したいのは 5 つ。

まとめて選べる          1 回の選択で複数のファイルが一覧に並ぶ
1 つずつ外せる          「外す」で選んだ 1 つだけが消え、送る欄（`#upload`）も同じになる
同じものは足さない      名前と大きさが同じなら足さず、理由を知らせる
上限を超えない          上限（`max_files`）を超える分は足さず、知らせる
すべて外せる            「すべて外す」で空に戻る

画面の HTML は実際のアプリが返したもの、スクリプトと CSS は配布する実物を使う。

Playwright とブラウザが無い環境では skip する（`AIJUDGE_REQUIRE_BROWSER=1` なら失敗）。
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest
from test_studentweb import World, _set_task
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


@pytest.fixture
def page(world: World):
    world.register("s2400001")
    world.login("s2400001")
    _set_task(world, accepted_suffixes=(".png", ".jpg"))
    html = world.client.get(f"/tasks/{world.task_version.id}").text

    def serve(route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        if path.startswith(STATIC_MOUNT + "/"):
            asset = ASSETS_DIR / path.removeprefix(STATIC_MOUNT + "/")
            if asset.is_file():
                route.fulfill(body=asset.read_bytes(), content_type=_type(asset.suffix))
                return
        if path == "/task":
            route.fulfill(body=html, content_type="text/html; charset=utf-8")
            return
        route.abort()

    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # ブラウザが無いだけ
            _unavailable(f"chromium is not installed: {error}")
        tab = browser.new_page()
        errors: list[str] = []
        tab.on("pageerror", lambda e: errors.append(str(e)))
        tab.route("**/*", serve)
        tab.goto(f"{ORIGIN}/task")
        yield tab
        assert errors == [], errors
        browser.close()


def _type(suffix: str) -> str:
    return {".js": "text/javascript", ".css": "text/css"}.get(suffix, "application/octet-stream")


def _files(tmp_path, count: int, prefix: str = "cert") -> list[str]:
    paths = []
    for n in range(count):
        path = tmp_path / f"{prefix}{n}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([n]) * (10 + n))
        paths.append(str(path))
    return paths


def _names(page) -> list[str]:
    return page.eval_on_selector_all(
        "#upload-list .file-name", "els => els.map(e => e.textContent)"
    )


def _sent(page) -> list[str]:
    """送信される欄（`#upload`）の中身。"""
    return page.evaluate("Array.from(document.getElementById('upload').files).map(f => f.name)")


def test_several_files_are_picked_at_once(page, tmp_path) -> None:
    page.set_input_files("#upload-picker", _files(tmp_path, 3))

    assert len(_names(page)) == 3
    assert _sent(page) == ["cert0.png", "cert1.png", "cert2.png"]
    assert page.is_visible("#upload-clear")


def test_one_file_can_be_removed(page, tmp_path) -> None:
    page.set_input_files("#upload-picker", _files(tmp_path, 3))

    page.click("#upload-list li:nth-child(2) .file-remove")

    assert _sent(page) == ["cert0.png", "cert2.png"]
    assert len(_names(page)) == 2


def test_picking_again_adds_and_skips_the_same_file(page, tmp_path) -> None:
    files = _files(tmp_path, 2)
    page.set_input_files("#upload-picker", files)

    page.set_input_files("#upload-picker", [files[1], *_files(tmp_path, 3, prefix="more")[:1]])

    assert _sent(page) == ["cert0.png", "cert1.png", "more0.png"]
    assert "同じファイルは追加済み" in page.text_content("#upload-notice")


def test_files_past_the_limit_are_not_added(page, tmp_path) -> None:
    page.set_input_files("#upload-picker", _files(tmp_path, 12))

    assert len(_sent(page)) == 10
    assert "10 ファイルまで" in page.text_content("#upload-notice")


def test_remove_all_empties_the_list(page, tmp_path) -> None:
    page.set_input_files("#upload-picker", _files(tmp_path, 2))

    page.click("#upload-clear")

    assert _sent(page) == []
    assert not page.is_visible("#upload-clear")
    assert page.is_visible("#upload-empty")
