"""試験は公開まで教員にしか見せない（`Task.confidential_until_open`）。

公開前の問題セットは教員と TA の両方に見えていた（#340）。課題なら正しいが、
試験では TA（多くは学生）が内容を先に知ること自体が漏洩の経路になる。

固定したいのは 5 つ。

TA に出ない     秘匿の課題は、公開前の TA の一覧にも課題ページにも出ず、出せない。
公開で出る      公開時刻を過ぎれば、TA にも見える（質問対応と採点のため）。
教員は見える    教員は公開前でも見られ、試しに出せる。
課題は従来どおり  秘匿でない課題は、TA にも公開前から見える（#340 を壊さない）。
URL でも出ない   学習者は公開前の課題ページを URL で開けない（以前は開けた）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_studentweb import COURSE, EXAMPLE_SOURCE, World, _set_task
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import Role

EMPTY_LIST = "公開されている課題がありません"


def _open_tomorrow(world: World, *, confidential: bool) -> None:
    _set_task(
        world,
        opens_at=datetime.now(UTC) + timedelta(days=1),
        confidential_until_open=confidential,
    )


def _submit(world: World):
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", EXAMPLE_SOURCE.read_bytes(), "text/plain")},
        follow_redirects=False,
    )


def _as(world: World, login: str, role: Role) -> None:
    world.register(login, role=role)
    world.login(login)


def test_an_assistant_does_not_see_a_confidential_set_before_it_opens(world: World) -> None:
    _as(world, "ta", Role.ASSISTANT)
    _open_tomorrow(world, confidential=True)

    body = world.client.get(f"/courses/{COURSE}").text

    assert EMPTY_LIST in body
    assert "公開前（動作確認）" not in body


def test_an_assistant_cannot_open_or_submit_to_it_by_url(world: World) -> None:
    """**一覧から外すだけでは制限にならない。** URL を知っていれば開ける。"""
    _as(world, "ta", Role.ASSISTANT)
    _open_tomorrow(world, confidential=True)

    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 404
    assert _submit(world).status_code == 404


def test_an_assistant_sees_it_once_it_opens(world: World) -> None:
    _as(world, "ta", Role.ASSISTANT)
    _set_task(
        world,
        opens_at=datetime.now(UTC) - timedelta(minutes=1),
        confidential_until_open=True,
    )

    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 200
    assert EMPTY_LIST not in world.client.get(f"/courses/{COURSE}").text


def test_an_instructor_sees_and_tries_it_before_it_opens(world: World) -> None:
    _as(world, "teacher", Role.INSTRUCTOR)
    _open_tomorrow(world, confidential=True)

    body = world.client.get(f"/courses/{COURSE}").text
    assert "公開前（動作確認）" in body
    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 200
    assert _submit(world).status_code == 303


def test_a_plain_set_is_still_open_to_an_assistant_before_it_opens(world: World) -> None:
    """秘匿でない課題は従来どおり（#340）── TA が先に読んで備えられる。"""
    _as(world, "ta", Role.ASSISTANT)
    _open_tomorrow(world, confidential=False)

    assert "公開前（動作確認）" in world.client.get(f"/courses/{COURSE}").text
    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 200
    assert _submit(world).status_code == 303


def test_a_learner_cannot_open_a_task_before_it_opens_by_url(world: World) -> None:
    """**以前は開けた。** 一覧からは外れていたが、課題ページが公開日時を見て
    いなかったので、URL を知っていれば公開前の問題文が読めた。"""
    _as(world, "s2400001", Role.LEARNER)
    _open_tomorrow(world, confidential=False)

    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 404
    assert _submit(world).status_code == 404
