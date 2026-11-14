"""撮影つきの試験で、エディタの画面からのファイル提出も共有を要すること（#705）。

課題の画面のファイル経路は撮る試験では学習者に閉じている
（`test_exam_screen_stills.test_the_file_route_is_closed_to_learners_in_a_screen_exam`）。
エディタの画面の添付（`/ide/tasks/{id}/upload`）が共有なしで通ると、共有を止めた
まま画像・PDF の答案を出せ、その閉じた意味が無くなる。ここではその経路でも

- 共有の記録が無い・共有を止めたあとは 409、共有中だけ 200
- 他人の共有中のセッション id を借りても 409（共有の状態は本人のセッションで見る）

になることを固定する。
"""

from __future__ import annotations

from test_exam_screen_stills import _share
from test_ide_routes import PNG, _editor, _learner, _start, _with_activity
from test_studentweb import COURSE, World
from test_studentweb import world as world  # フィクスチャを借りる


def _attach(world: World, session_id: str | None):
    """エディタの画面の添付で、画像を 1 枚出す（`ide_session_id` はフォームの欄）。"""
    data = {} if session_id is None else {"ide_session_id": session_id}
    return world.client.post(
        f"/ide/tasks/{world.task_version.id}/upload",
        files={"upload": ("answer.png", PNG, "image/png")},
        data=data,
    )


def _screen_exam(world: World, tmp_path) -> None:
    _editor(world, screen_capture=True, accepted_suffixes=(".c", ".png"))
    _with_activity(world, tmp_path)


def test_attaching_a_file_needs_the_screen_to_be_shared(world: World, tmp_path) -> None:
    """**共有中だけ出せる。** 記録なし・止めたあとは、`/ide/.../submit` と同じく 409。"""
    _screen_exam(world, tmp_path)
    _learner(world)

    assert _attach(world, None).status_code == 409, "セッションなしで通った"
    session_id = _start(world).json()["session_id"]
    refused = _attach(world, session_id)
    assert refused.status_code == 409, "共有の記録なしで通った"
    assert "共有" in refused.json()["detail"]

    assert _share(world, session_id, "sharing").status_code == 200
    accepted = _attach(world, session_id)
    assert accepted.status_code == 200, accepted.text

    assert _share(world, session_id, "stopped").status_code == 200
    # 同じ中身でも、通れば重複として畳まれて 200 になる。409 は関門で止まった印。
    assert _attach(world, session_id).status_code == 409, "共有の停止後に通った"


def test_someone_elses_shared_session_does_not_count(world: World, tmp_path) -> None:
    """**他人の共有中のセッション id を借りても通らない。** 共有の状態は本人の
    セッションでだけ見る。借りられると、隣の席の共有で自分は撮られずに出せる。"""
    _screen_exam(world, tmp_path)
    _learner(world)
    lent = _start(world).json()["session_id"]
    assert _share(world, lent, "sharing").status_code == 200

    world.register("s2400002")
    world.login("s2400002")
    response = _attach(world, lent)

    assert response.status_code == 409, "他人の共有で提出できた"
    with world.database.unit_of_work() as uow:
        assert len(uow.submissions.list_for_course(COURSE)) == 0
