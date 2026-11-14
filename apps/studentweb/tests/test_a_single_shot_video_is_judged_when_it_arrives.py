"""1 発の動画送信は、本文を受け終えた時刻で受付を判定し直す。

本文を読む前の関門は 1 回だけで、受付終了の直前に送り始めて本文を少しずつ
流せば、受付終了後に送り終えた動画が受理されていた。分割アップロードの確定
（`test_finishing_after_the_window_closed_is_refused`）と同じく、提出が成立
するのは本文を受け終えた瞬間である。

固定したいのは 3 つ。

窓で閉じる     送っている間に受付が終わったら 409。提出は作らず、動画も残さない。
学内限定も     送っている間に学内の範囲から外れたら同じく断る。
開いていれば   送り終えた時点で開いていれば、これまでどおり受理する。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from test_video_upload import COURSE, TENANT, World
from test_video_upload import world as world  # フィクスチャを借りる

from aijudge_identity import CampusNetworkSettings

CAMPUS = "133.83.80.0/24"
ELSEWHERE = "10.0.0.0/8"
INSIDE = "133.83.80.110"


def _trickle(between: Callable[[], None]) -> Iterator[bytes]:
    """本文の途中で `between` を走らせる（関門を通った後・受け終える前）。"""
    yield b"PRETEND-"
    between()
    yield b"MP4-BYTES"


def _post_trickled(world: World, between: Callable[[], None], *, ip: str | None = None):
    headers = {"Content-Type": "application/octet-stream"}
    if ip is not None:
        headers["x-forwarded-for"] = ip
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit-video?filename=demo.mp4",
        content=_trickle(between),
        headers=headers,
        follow_redirects=False,
    )


def _set_task(world: World, **update) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update=update))
        uow.commit()


def _set_campus(world: World, cidr: str) -> None:
    with world.database.unit_of_work() as uow:
        uow.identity.save_campus_networks(CampusNetworkSettings(tenant_id=TENANT, cidrs=(cidr,)))
        uow.commit()


def _submissions(world: World) -> list:
    with world.database.unit_of_work() as uow:
        return list(uow.submissions.list_for_course(COURSE))


def test_a_window_that_closes_while_the_body_streams_refuses_it(world: World) -> None:
    """**これが抜けていた。** 始めた時刻ではなく、受け終えた時刻で判定する。"""
    world.register_and_login()
    started: list[bool] = []

    def close_the_window() -> None:
        started.append(True)
        now = datetime.now(UTC)
        _set_task(world, due_at=now - timedelta(days=2), accepts_until=now - timedelta(days=1))

    response = _post_trickled(world, close_the_window)

    # 本文の途中まで届いていた（＝始めの関門は通っていた）ことを確かめておく。
    assert started == [True]
    assert response.status_code == 409, response.text
    assert "受付は終了しました" in response.json()["detail"]
    assert _submissions(world) == []
    # 書き終えた動画を残さない。
    assert list(world.video_dir.rglob("*.mp4")) == []


def test_leaving_the_campus_ranges_while_the_body_streams_refuses_it(world: World) -> None:
    """学内限定も同じ時刻で判定する（窓だけ判定し直すと、こちらが抜け道になる）。"""
    world.register_and_login()
    _set_task(world, campus_only=True)
    _set_campus(world, CAMPUS)

    response = _post_trickled(world, lambda: _set_campus(world, ELSEWHERE), ip=INSIDE)

    assert response.status_code == 409, response.text
    assert _submissions(world) == []
    assert list(world.video_dir.rglob("*.mp4")) == []


def test_a_window_still_open_when_the_body_arrives_accepts_it(world: World) -> None:
    """判定し直すことで、開いている間の提出を止めていないこと。"""
    world.register_and_login()

    response = _post_trickled(world, lambda: None)

    assert response.status_code == 303, response.text
    (submission,) = _submissions(world)
    assert submission.artifacts[0].byte_size == len(b"PRETEND-MP4-BYTES")
