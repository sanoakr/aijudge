"""締切の無い課題の動画は小さく絞る（ADR 0020）。

**上限と保存期間は一対である。** 締切の無い課題の動画は提出から 1 年残り、
しかも回ごとにまとめて消せない（共通の起点が無い）。長く置くぶん、受け付ける
大きさを下げて釣り合わせる ── どちらか片方だけを変えると、砂場・自習用に
数 GB が積み上がる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

# テストのディレクトリには `__init__.py` が無いので、同じディレクトリの
# module としてそのまま読む。
from test_video_upload import COURSE, World

from aijudge_core import Task
from aijudge_core.ids import TaskId, TaskVersionId, new_id
from aijudge_studentweb.app import MAX_VIDEO_BYTES, MAX_VIDEO_BYTES_WITHOUT_DEADLINE


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def an_undated_task(world: World) -> TaskVersionId:
    """締切を持たない課題を 1 つ足し、その版を返す。"""
    version = world.task_version.model_copy(
        update={"id": TaskVersionId(new_id("tsv")), "task_id": TaskId(new_id("tsk"))}
    )
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=version.task_id, course_id=COURSE, title="自習用"))
        uow.tasks.save_version(version)
        uow.commit()
    return version.id


def test_the_default_limits_are_a_pair(world: World) -> None:
    # 5 GiB と 256 MiB。**期間の長い側が小さい。**
    assert MAX_VIDEO_BYTES == 5 * 1024 * 1024 * 1024
    assert MAX_VIDEO_BYTES_WITHOUT_DEADLINE == 256 * 1024 * 1024
    assert MAX_VIDEO_BYTES_WITHOUT_DEADLINE < MAX_VIDEO_BYTES


def test_a_task_without_a_deadline_gets_the_smaller_limit(world: World) -> None:
    with world.database.unit_of_work() as uow:
        dated = uow.tasks.get_task(world.task_version.task_id)
        undated = Task(id=TaskId(new_id("tsk")), course_id=COURSE, title="自習用")

    assert world.app.video_limit_for(dated) == world.app.max_video_bytes
    assert world.app.video_limit_for(undated) == world.app.max_video_bytes_without_deadline


def test_the_smaller_limit_is_enforced_on_upload(tmp_path: Path) -> None:
    """**断るのは受け取りながら**である。全体をメモリに載せてから測らない。"""
    world = World(tmp_path)
    try:
        # 締切あり 2000 バイト / 締切なし 500 バイトにして、差だけを見る。
        world.app.max_video_bytes_without_deadline = 500
        world.register_and_login()
        version_id = an_undated_task(world)

        res = world.client.post(
            f"/tasks/{version_id}/submit-video?filename=demo.mp4",
            content=b"x" * 1200,
            headers={"Content-Type": "application/octet-stream"},
            follow_redirects=False,
        )

        assert res.status_code == 413
        assert "500" in res.text
        # 中途半端なファイルを残さない。
        assert list(world.video_dir.rglob("*.mp4")) == []
        assert list(world.video_dir.rglob("*.partial")) == []
    finally:
        world.close()


def test_the_same_size_is_accepted_when_the_task_has_a_deadline(tmp_path: Path) -> None:
    """同じ大きさでも、締切のある課題なら通る（上限は課題で決まる）。"""
    world = World(tmp_path)
    try:
        world.app.max_video_bytes_without_deadline = 500
        world.register_and_login()

        res = world.post_video(b"x" * 1200)

        assert res.status_code == 303, res.text
    finally:
        world.close()


def test_the_task_page_shows_the_limit_that_applies(world: World) -> None:
    """画面に出す値も課題ごと ── 一律に書くと、数 GB 送ってから断られる。"""
    world.register_and_login()
    version_id = an_undated_task(world)

    page = world.client.get(f"/tasks/{version_id}")

    assert page.status_code == 200
    megabytes = round(MAX_VIDEO_BYTES_WITHOUT_DEADLINE / 1024 / 1024)
    assert f"上限 {megabytes} MB" in page.text


def test_the_undated_video_expires_a_year_after_the_submission() -> None:
    """上限と対になっている期間そのもの（コア側の規則）。"""
    from aijudge_core import video_retention_expires_at

    submitted_at = datetime(2026, 10, 1, tzinfo=UTC)
    assert video_retention_expires_at(None, submitted_at=submitted_at) == datetime(
        2027, 10, 1, tzinfo=UTC
    )
