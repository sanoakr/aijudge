"""「提出」の画面に、いまの採点の待ちを出す（2026-11-13）。

固定したいのは 2 つ。

待ちが見える      まだ取られていない提出があれば、段階ごとの「待ち」に数える。
空なら空と言う    全部終われば「待っている採点はありません」。
"""

from __future__ import annotations

from test_console import COURSE, World
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _page(world: World) -> str:
    return world.client.get(f"/courses/{COURSE}/submissions").text


def test_a_waiting_submission_shows_in_the_queue_load(world: World) -> None:
    learner = world.register("s2400001", role=Role.LEARNER)
    world.register("instructor", role=Role.INSTRUCTOR)
    world.submit(learner)
    world.login("instructor")

    body = _page(world)
    assert 'data-live="sub-queue"' in body
    assert "テスト実行: 処理中 0・待ち 1" in body


def test_an_empty_queue_says_so(world: World) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")

    assert "待っている採点はありません" in _page(world)
