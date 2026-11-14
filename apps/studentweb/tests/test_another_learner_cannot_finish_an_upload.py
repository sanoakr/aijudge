"""他人の分割アップロードは確定できないこと（#705）。

`test_a_video_upload_resumes_after_a_break.test_another_learner_cannot_see_or_continue_it`
は位置の問い合わせと追記だけを見ていた。確定（`POST /uploads/{id}/finish`）の所有者の
確認は `_own_session` だけで、その先は呼び出した人の名義で提出を作り、受け皿を捨てる。
`_video_gate` は課題が見えるか・受付の窓しか見ないので、同じコースの別の学習者なら
通ってしまう。ここが抜けると、送りかけの動画を他人が自分の提出として確定でき、
本人の受け皿は消える。
"""

from __future__ import annotations

from test_a_video_upload_resumes_after_a_break import _append, _start
from test_video_upload import COURSE, PASSWORD, SESSION_COOKIE, World
from test_video_upload import world as world  # フィクスチャを借りる


def _login_again(world: World, login: str) -> None:
    """登録済みの学習者でログインし直す（`register_and_login` は登録もするので使えない）。"""
    res = world.client.post(
        "/auth/local", data={"login": login, "password": PASSWORD}, follow_redirects=False
    )
    assert res.status_code == 303, res.text
    world.client.cookies.set(SESSION_COOKIE, res.cookies[SESSION_COOKIE])


def test_another_learner_cannot_finish_it(world: World) -> None:
    """id を知っていても他人の受け皿は確定できず、**在ることも知らせない**（404）。
    本人の受け皿は残り、本人はそのあと確定できる。"""
    world.register_and_login("s2400001")
    upload_id = _start(world)["upload_id"]
    _append(world, upload_id, b"mine", offset=0)

    world.register_and_login("s2400002")
    assert world.client.post(f"/uploads/{upload_id}/finish").status_code == 404
    with world.database.unit_of_work() as uow:
        assert len(uow.submissions.list_for_course(COURSE)) == 0, "他人の名義で提出ができた"

    _login_again(world, "s2400001")
    done = world.client.post(f"/uploads/{upload_id}/finish")

    assert done.status_code == 200, done.text
    with world.database.unit_of_work() as uow:
        (submission,) = uow.submissions.list_for_course(COURSE)
    assert submission.artifacts[0].byte_size == len(b"mine")
