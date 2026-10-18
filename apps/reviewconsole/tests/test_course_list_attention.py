"""担当コースの一覧に「要対応」の札を出す（2026-10-19）。

右の件数を目で全行追わないと、どのコースに用があるのか分からなかった。固定したいのは 3 つ。

帯と同じ数    札の内訳は、開いた先の帯の項目名と数字そのもの
無ければ出さない  人待ちが 0 件のコースには札を付けない
区画は残す      0 件でも `data-live` の区画は置く（後から増えたら差し替わる）
"""

from __future__ import annotations

import re

from test_console import World, _request_review, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _attention(body: str) -> str | None:
    """担当コースの行に付いた「要対応」の札の文字列（無ければ None）。"""
    region = re.search(
        r'<span class="attention" data-live="attn-[^"]+">(.*?)</span>\s*<span', body, re.S
    )
    assert region is not None, "担当コースの行に要対応の区画が無い"
    pill = re.search(r'<span class="pill attn">(.*?)</span>', region.group(1), re.S)
    return None if pill is None else " ".join(pill.group(1).split())


def test_a_course_with_nothing_waiting_has_no_badge(world: World) -> None:
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")

    assert _attention(world.client.get("/").text) is None


@needs_c_compiler
def test_the_badge_names_what_waits_with_the_rails_numbers(blind_world: World) -> None:
    first = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.submit(blind_world.register("s2400002", role=Role.LEARNER))
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()
    _request_review(blind_world, first.submission.id)

    badge = _attention(blind_world.client.get("/").text)
    assert badge is not None and badge.startswith("要対応")
    assert "再確認の依頼 1" in badge
    assert "blind 採点 2" in badge
