"""試験中の画面の静止画（ADR 0027・#444）の受け口と、提出の関門。

固定したいのは次のこと。

- 共有の状態が `sharing` のときだけ、学習者の手動の提出を受ける（止まっていれば 409）
- 画像が届いているかは見ない（通信の不調で試験を止めない）
- 教員・TA の動作確認と、撮らない課題は止めない
- ファイルの経路（課題の画面の提出）は、撮る試験では学習者に閉じる
- 静止画は JPEG だけ、自分のセッションにだけ、決まった種類だけ受け、記録と同じ
  ディレクトリに置く（purge で一緒に消える）
"""

from __future__ import annotations

from test_ide_routes import _editor, _learner, _start, _submit, _with_activity
from test_studentweb import World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import Role

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _share(world: World, session_id: str, state: str, surface: str = "monitor"):
    return world.client.post(
        "/ide/screen/state", json={"session_id": session_id, "state": state, "surface": surface}
    )


def _still(world: World, session_id: str, kind: str = "random", body: bytes = JPEG):
    return world.client.post(
        f"/ide/screen?session_id={session_id}&kind={kind}&t=1234.5",
        content=body,
        headers={"content-type": "image/jpeg"},
    )


def test_submitting_needs_the_screen_to_be_shared(world: World, tmp_path) -> None:
    _editor(world, screen_capture=True)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    assert _submit(world, ide_session_id=session_id).status_code == 409, "共有の記録なしで通った"
    assert _share(world, session_id, "sharing").status_code == 200
    assert _submit(world, ide_session_id=session_id).status_code == 200

    assert _share(world, session_id, "stopped").status_code == 200
    refused = _submit(world, ide_session_id=session_id)
    assert refused.status_code == 409
    assert "共有" in refused.json()["detail"]


def test_a_submission_without_a_session_is_refused(world: World, tmp_path) -> None:
    _editor(world, screen_capture=True)
    _learner(world)
    assert _submit(world).status_code == 409


def test_a_task_without_screen_capture_is_unaffected(world: World, tmp_path) -> None:
    _editor(world)
    _learner(world)
    assert _submit(world).status_code == 200


def test_staff_can_try_the_exam_without_sharing(world: World) -> None:
    _editor(world, screen_capture=True)
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")
    assert _submit(world).status_code == 200


def test_the_file_route_is_closed_to_learners_in_a_screen_exam(world: World) -> None:
    """課題の画面からファイルで出せば撮影を迂回できる。**学習者には閉じる**。"""
    _editor(world, screen_capture=True, file_upload=True)
    _learner(world)
    response = world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", b"int main(void){return 0;}", "text/x-c")},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_a_still_is_stored_with_the_activity(world: World, tmp_path) -> None:
    from aijudge_ide import ActivityFiles

    _editor(world, screen_capture=True)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    stored = _still(world, session_id, "paste_before")
    assert stored.status_code == 201

    with world.database.unit_of_work() as uow:
        session = uow.ide_activity.get_session(session_id)
    files = ActivityFiles(tmp_path / "activity")
    (still,) = files.stills(session)
    assert still.kind == "paste_before" and still.t == 1234
    assert files.read_still(session, still.name) == JPEG
    # 記録と同じセッションのディレクトリの下（purge で一緒に消える）。
    assert (files.session_dir(session) / "stills" / still.name).is_file()


def test_only_jpegs_of_known_kinds_for_your_own_session(world: World, tmp_path) -> None:
    _editor(world, screen_capture=True)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    assert _still(world, session_id, body=b"<svg/>").status_code == 400
    assert _still(world, session_id, kind="webcam").status_code == 400
    assert _still(world, "ide_" + "0" * 32).status_code == 404

    world.register("s2400002")
    world.login("s2400002")
    assert _still(world, session_id).status_code == 404, "他人のセッションに書けた"
    assert _share(world, session_id, "sharing").status_code == 404
