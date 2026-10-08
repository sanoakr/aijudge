"""教員画面の上部バーの「オンライン N 人」（2026-10-08）。

固定したいのは 5 つ。

教員の画面にだけ出る    ログイン前の画面には出ない。学習者の画面（別のアプリ）には出さない
人数だけ出す            名前は出ない（ひとり分の記録は JSON にも画面にも無い）
操作が数えられる        画面を使った人が数に入る
自動更新は数えない      `X-Aijudge-Live` の取得は最終操作を記録しない
取り直せる              `/online-users` が人数を返す
"""

from __future__ import annotations

from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role

LIVE = {"X-Aijudge-Live": "1"}


def _fresh(world: World) -> None:
    """人数の使い回し（10 秒）を捨てる。"""
    world.console._active_users.clear()


def _online(world: World, client) -> dict:
    _fresh(world)
    return client.get("/online-users", headers=LIVE).json()


def test_the_bar_shows_the_count_only_after_login(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get("/").text
    assert 'id="online-users"' in page and "オンライン" in page
    assert 'data-url="' in page and "/online-users" in page

    from fastapi.testclient import TestClient

    from aijudge_reviewconsole.app import create_app

    anonymous = TestClient(create_app(world.console)).get("/login").text
    assert 'id="online-users"' not in anonymous, "ログイン前の画面に出ている"


def test_the_endpoint_gives_numbers_only(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.get("/")  # 操作する

    data = _online(world, client)

    assert set(data) == {"total", "staff", "learners", "window_minutes"}, "人数以外が混ざっている"
    assert data["window_minutes"] == 5
    assert data["total"] == 1 and data["staff"] == 1 and data["learners"] == 0


def test_a_person_using_the_console_is_counted(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("student", Role.LEARNER)
    teacher = world.client("teacher")
    teacher.get("/")
    world.client("student").get("/")  # 学習者は入れなくても、画面を使った（操作した）

    data = _online(world, teacher)

    assert data["total"] == 2 and data["learners"] == 1 and data["staff"] == 1


def test_a_background_refresh_is_not_an_operation(world: World) -> None:
    """**開いたままの画面が全員「使用中」に見えない**ように、自動更新の取得は数えない。"""
    world.register("watcher", Role.INSTRUCTOR)
    world.register("teacher", Role.INSTRUCTOR)
    watcher = world.client("watcher")
    teacher = world.client("teacher")
    for _ in range(3):
        watcher.get("/", headers=LIVE)
        watcher.get("/online-users", headers=LIVE)
    teacher.get("/")

    data = _online(world, teacher)

    assert data["total"] == 1, "自動更新の取得だけの人を数えている"


def test_the_endpoint_needs_a_login(world: World) -> None:
    from fastapi.testclient import TestClient

    from aijudge_reviewconsole.app import create_app

    response = TestClient(create_app(world.console)).get("/online-users", headers=LIVE)
    assert response.status_code in (401, 303, 307)
