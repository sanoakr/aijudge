"""動画の 2 つの経路は、学内限定を同じように断る（#333・#119）。

1 発の送信（`POST /tasks/{id}/submit-video`）は分割アップロードの関門
（`_video_gate`）を通らず、判定を写して持っていた。写すときに学内限定だけが
落ち、**学内限定の動画課題に学外から出せた**。

固定したいのは 3 つ。

同じ関門     学外からは、1 発の送信も分割アップロードの開始も 409。
学内は通す   関門を通すようにしたことで、学内からの提出が止まっていない。
置き場       1 発の送信は分割アップロードの置き場を要らない（関門を共有した
             ことで、要らないものまで要求するようになっていない）。
"""

from __future__ import annotations

from pathlib import Path

from test_video_upload import TENANT, World
from test_video_upload import world as world  # フィクスチャを借りる

from aijudge_identity import CampusNetworkSettings

CAMPUS = ("133.83.80.0/24",)
INSIDE = "133.83.80.110"
OUTSIDE = "82.26.195.14"


def _restrict(world: World) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update={"campus_only": True}))
        uow.identity.save_campus_networks(CampusNetworkSettings(tenant_id=TENANT, cidrs=CAMPUS))
        uow.commit()


def _post_video_from(world: World, ip: str):
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit-video?filename=demo.mp4",
        content=b"PRETEND-MP4-BYTES",
        headers={"Content-Type": "application/octet-stream", "x-forwarded-for": ip},
        follow_redirects=False,
    )


def test_a_single_shot_video_from_outside_is_refused(world: World) -> None:
    """**これが漏れていた経路。** 学外からは 409 で、動画も残らない。"""
    world.register_and_login()
    _restrict(world)

    response = _post_video_from(world, OUTSIDE)

    assert response.status_code == 409, response.text
    assert "学内からのみ" in response.text
    assert list(world.video_dir.rglob("*.mp4")) == []


def test_a_resumable_upload_from_outside_is_refused_the_same_way(world: World) -> None:
    """分割アップロードの側は元から断っていた。両経路が同じ答えを返すこと。"""
    world.register_and_login()
    _restrict(world)

    response = world.client.post(
        f"/tasks/{world.task_version.id}/uploads?filename=demo.mp4",
        headers={"x-forwarded-for": OUTSIDE},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == _post_video_from(world, OUTSIDE).json()["detail"]


def test_a_single_shot_video_from_campus_is_accepted(world: World) -> None:
    world.register_and_login()
    _restrict(world)

    assert _post_video_from(world, INSIDE).status_code == 303


def test_a_single_shot_video_does_not_need_the_resumable_store(tmp_path: Path) -> None:
    """1 発の送信が要るのは動画の置き場だけ。

    関門を共有したときに、分割アップロードの置き場（`upload_sessions`）まで
    要求して 501 を返すようになっていないこと。
    """
    world = World(tmp_path)
    try:
        world.app.upload_sessions = None
        world.register_and_login()

        response = world.post_video(b"PRETEND-MP4-BYTES")

        assert response.status_code == 303, response.text
    finally:
        world.close()
