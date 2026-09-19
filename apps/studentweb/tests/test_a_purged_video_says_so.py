"""消した動画を開いたときに何が出るか（ADR 0020）。

**404 にしない。** 消去は運用の結果であって不具合ではないので、「見つかり
ません」と同じ顔で出すと、学習者は区別できず問い合わせ先も違う。410 と、
理由の書かれた文面を出す。

提出の画面も同じで、**埋め込みを先に試さない** ── 壊れた再生器が出るだけで
理由が出ない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

# テストのディレクトリには `__init__.py` が無い（パッケージではない）ので、
# 同じディレクトリの module としてそのまま読む。
from test_video_upload import World

from aijudge_core import PURGED_MESSAGE

PURGED_AT = datetime(2027, 4, 2, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def a_purged_video(world: World) -> tuple[str, str]:
    """動画を 1 件出し、保存期間を過ぎたものとして消す。"""
    world.register_and_login()
    location = world.post_video(b"PRETEND-MP4-BYTES").headers["location"]
    submission_id = location.split("/submissions/")[1].split("?")[0]
    with world.database.unit_of_work() as uow:
        submission = uow.submissions.get(submission_id)
        artifact = submission.artifacts[0]
        world.app.video_store.delete(artifact.storage_key)
        uow.submissions.mark_artifacts_purged([(submission.id, artifact.id)], purged_at=PURGED_AT)
        uow.commit()
    return submission_id, str(artifact.id)


def test_downloading_a_purged_video_is_gone_not_missing(world: World) -> None:
    submission_id, artifact_id = a_purged_video(world)

    res = world.client.get(f"/submissions/{submission_id}/artifacts/{artifact_id}")

    # 410 は「あったが、もう無い」。404 との差がそのまま問い合わせ先の差になる。
    assert res.status_code == 410
    assert PURGED_MESSAGE in res.text


def test_the_submission_page_explains_it_instead_of_embedding_a_player(world: World) -> None:
    submission_id, _ = a_purged_video(world)

    res = world.client.get(f"/submissions/{submission_id}")

    assert res.status_code == 200
    assert PURGED_MESSAGE in res.text
    # **再生器を出さない。** 出すと、読み込みに失敗した画面が理由の代わりになる。
    assert (
        f'<video controls preload="metadata"\n         src="/submissions/{submission_id}'
        not in res.text
    )


def test_a_video_that_is_still_within_its_window_is_untouched(world: World) -> None:
    """消していないものまで同じ文面にしない。"""
    world.register_and_login()
    location = world.post_video(b"PRETEND-MP4-BYTES").headers["location"]
    submission_id = location.split("/submissions/")[1].split("?")[0]

    res = world.client.get(f"/submissions/{submission_id}")

    assert PURGED_MESSAGE not in res.text
