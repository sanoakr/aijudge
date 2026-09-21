"""切れたところから続けられる動画提出（#119）。

固定したいのは 7 つ。

続きから       切断しても、送り直すのは残りだけ。提出は 1 つになる。
位置を答える   サーバは「いま何バイト持っているか」を返す。再開はそこから。
ずれは断る     位置が合わない追記は 409 で、サーバの位置を返す（やり直しではない）。
他人は触れない 受け皿の id を知っていても、他人のものには書けない・見えない。
関門は同じ     受付期間・学内限定・拡張子・上限は 1 発送信と同じ判定を通る。
確定で閉じる   受付終了後に確定はできない（提出が成立するのは確定の瞬間）。
二度押し       確定を 2 回押しても提出は 1 つ（受け皿の id が冪等キー）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_video_upload import COURSE, World
from test_video_upload import world as world  # フィクスチャを借りる

OCTET = {"Content-Type": "application/octet-stream"}


def _start(world: World, *, filename: str = "demo.mp4") -> dict:
    response = world.client.post(f"/tasks/{world.task_version.id}/uploads?filename={filename}")
    assert response.status_code == 200, response.text
    return response.json()


def _append(world: World, upload_id: str, body: bytes, *, offset: int):
    return world.client.patch(f"/uploads/{upload_id}?offset={offset}", content=body, headers=OCTET)


def _set_task(world: World, **update) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update=update))
        uow.commit()


def test_an_upload_broken_in_the_middle_continues_from_there(world: World) -> None:
    """**これがこの仕組みの目的。** 90% で切れても、残りだけ送れば済む。"""
    world.register_and_login()
    started = _start(world)
    upload_id = started["upload_id"]

    assert _append(world, upload_id, b"first", offset=0).json()["offset"] == 5
    # ここで切断。クライアントは位置を訊き直す。
    asked = world.client.get(f"/uploads/{upload_id}")
    assert asked.status_code == 200
    assert asked.json()["offset"] == 5

    assert _append(world, upload_id, b"second", offset=5).json()["offset"] == 11
    done = world.client.post(f"/uploads/{upload_id}/finish")

    assert done.status_code == 200, done.text
    with world.database.unit_of_work() as uow:
        submissions = uow.submissions.list_for_course(COURSE)
    assert len(submissions) == 1
    artifact = submissions[0].artifacts[0]
    assert artifact.byte_size == len(b"firstsecond")
    # 中身がつながっていること（**穴も重なりも無い**）。
    assert world.app.video_store.get(artifact.storage_key) == b"firstsecond"


def test_a_chunk_at_the_wrong_place_is_refused_with_the_server_position(world: World) -> None:
    """**ずれたまま書かない。** 穴の空いた動画が「正常」として残る。"""
    world.register_and_login()
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"0123456789", offset=0)

    response = _append(world, upload_id, b"xxx", offset=3)

    assert response.status_code == 409
    # クライアントはここから続ければよい（最初からやり直さない）。
    assert response.json()["offset"] == 10


def test_the_limit_holds_across_chunks(world: World) -> None:
    """分割すれば通る、では上限の意味が無い（`max_video_bytes=2000`）。"""
    world.register_and_login()
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"x" * 1500, offset=0)

    response = _append(world, upload_id, b"x" * 600, offset=1500)

    assert response.status_code == 413


def test_another_learner_cannot_see_or_continue_it(world: World) -> None:
    """id を知っていても、他人の提出には書けない。**在ることも知らせない。**"""
    world.register_and_login("s2400001")
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"mine", offset=0)
    world.register_and_login("s2400002")

    assert world.client.get(f"/uploads/{upload_id}").status_code == 404
    assert _append(world, upload_id, b"theirs", offset=4).status_code == 404


def test_starting_one_is_refused_before_the_window_opens(world: World) -> None:
    """**関門は 1 発送信と同じ。** 片方でしか効かない制限を作らない。"""
    world.register_and_login()
    _set_task(world, submissions_open_at=datetime.now(UTC) + timedelta(hours=2))

    response = world.client.post(f"/tasks/{world.task_version.id}/uploads?filename=demo.mp4")

    assert response.status_code == 409
    assert "まだ提出できません" in response.json()["detail"]


def test_a_format_the_task_does_not_accept_is_refused_at_the_start(world: World) -> None:
    """3 GB を送り終えてから断るのでは遅い。"""
    world.register_and_login()

    response = world.client.post(f"/tasks/{world.task_version.id}/uploads?filename=demo.mov")

    assert response.status_code == 400


def test_finishing_after_the_window_closed_is_refused(world: World) -> None:
    """**提出が成立するのは確定の瞬間。** 始めた時刻ではない。"""
    world.register_and_login()
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"late", offset=0)
    now = datetime.now(UTC)
    _set_task(world, due_at=now - timedelta(days=2), accepts_until=now - timedelta(days=1))

    response = world.client.post(f"/uploads/{upload_id}/finish")

    assert response.status_code == 409
    assert "受付は終了しました" in response.json()["detail"]


def test_finishing_twice_makes_one_submission(world: World) -> None:
    """確定を 2 回押しても提出は 1 つ（受け皿の id が冪等キー）。"""
    world.register_and_login()
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"once", offset=0)

    first = world.client.post(f"/uploads/{upload_id}/finish")
    second = world.client.post(f"/uploads/{upload_id}/finish")

    assert first.status_code == 200
    # 2 回目は受け皿が片付いているので「無い」と答える。**提出は増えない。**
    assert second.status_code == 404
    with world.database.unit_of_work() as uow:
        assert len(uow.submissions.list_for_course(COURSE)) == 1


def test_an_empty_upload_is_not_a_submission(world: World) -> None:
    """0 バイトを提出にすると、採点が「読めない」として人に回る。"""
    world.register_and_login()
    upload_id = _start(world)["upload_id"]

    response = world.client.post(f"/uploads/{upload_id}/finish")

    assert response.status_code == 400
    assert "空です" in response.json()["detail"]


def test_a_deployment_without_video_says_so(tmp_path) -> None:
    """動画を受けない配備では、分割の口も開かない。"""
    without = World(tmp_path, with_video=False)
    try:
        without.register_and_login()

        response = without.client.post(
            f"/tasks/{without.task_version.id}/uploads?filename=demo.mp4"
        )

        assert response.status_code == 501
    finally:
        without.close()


def test_the_receipt_says_how_big_a_chunk_should_be(world: World) -> None:
    """**分割の大きさはサーバが決める。** 配備ごとに回線とディスクが違う。"""
    world.register_and_login()

    started = _start(world)

    assert started["chunk_size"] > 0
    assert started["max_bytes"] == 2000
    assert started["offset"] == 0
