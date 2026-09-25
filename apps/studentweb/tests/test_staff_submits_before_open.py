"""教員・TA は公開前の課題にも出せる（#340）。

固定したいのは 5 つ。

出せる         `NOT_OPEN` でも教員・TA は受け付ける（`is_trial` なので数えない）。
学習者は不可   同じ課題を学習者が出すと、今までどおり 409 で断る。
一覧に出る     出せるのに一覧に無いと、URL を直接叩くしかない。
公開前と書く   開けたことをもって「公開済み」と読まれない。
「もう」は別   受付終了（`CLOSED`）は教員・TA にも開けない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_studentweb import COURSE, EXAMPLE_SOURCE, World, _set_task
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _submit(world: World) -> object:
    return world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", EXAMPLE_SOURCE.read_bytes(), "text/plain")},
        follow_redirects=False,
    )


def test_an_instructor_can_submit_before_the_window_opens(world: World) -> None:
    """公開前こそ動作確認したい ── 採点が意図どおり動くかを実物で確かめる。"""
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")
    _set_task(world, submissions_open_at=datetime.now(UTC) + timedelta(hours=2))

    response = _submit(world)
    assert response.status_code == 303, response.text

    # 数えないほうは変わらない（#108）。出したものは試行である。
    with world.database.unit_of_work() as uow:
        (submission,) = uow.submissions.list_for_course(COURSE)
    assert submission.submitted_as is Role.INSTRUCTOR
    assert submission.is_trial


def test_an_assistant_can_submit_before_the_window_opens(world: World) -> None:
    """TA も同じ ── `is_trial` が真になる役割はどちらも同じ扱い。"""
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")
    _set_task(world, submissions_open_at=datetime.now(UTC) + timedelta(hours=2))

    assert _submit(world).status_code == 303


def test_a_learner_is_still_refused_before_the_window_opens(world: World) -> None:
    """広げたのは役割であって、受付そのものではない。"""
    world.register("s2400001")
    world.login("s2400001")
    _set_task(world, submissions_open_at=datetime.now(UTC) + timedelta(hours=2))

    response = _submit(world)
    assert response.status_code == 409
    assert "まだ提出できません" in response.json()["detail"]


def test_the_closed_end_stays_shut_for_an_instructor(world: World) -> None:
    """**広げるのは「まだ」の側だけ**（#73）。過去の回に後から出せてはならない。"""
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")
    now = datetime.now(UTC)
    _set_task(
        world,
        opens_at=now - timedelta(days=14),
        due_at=now - timedelta(days=8),
        accepts_until=now - timedelta(days=7),
    )

    response = _submit(world)
    assert response.status_code == 409
    assert "受付は終了しました" in response.json()["detail"]


def test_the_task_page_offers_the_form_and_says_it_is_before_the_opening(world: World) -> None:
    """出せるのに欄が無いのでは確かめられない。**そして公開前だと書く。**"""
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")
    _set_task(world, submissions_open_at=datetime.now(UTC) + timedelta(hours=2))

    body = world.client.get(f"/tasks/{world.task_version.id}").text
    assert 'type="file"' in body, "出せるのに提出欄が無い"
    assert "まだ提出開始前です" in body
    assert "<strong>まだ提出できません。</strong>" not in body, "断りの表示が残っている"


def test_a_set_before_its_opening_is_listed_for_an_instructor(world: World) -> None:
    """出せるのに一覧に無いと、URL を直接叩くしかない。"""
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")
    _set_task(world, opens_at=datetime.now(UTC) + timedelta(days=1))

    body = world.client.get(f"/courses/{COURSE}").text
    assert "公開されている課題がありません" not in body
    assert "学生には未公開" in body


def test_a_set_before_its_opening_is_still_hidden_from_a_learner(world: World) -> None:
    """学習者の一覧は変わらない ── 公開日時を持たせた意味が消えてはいけない。"""
    world.register("s2400001")
    world.login("s2400001")
    _set_task(world, opens_at=datetime.now(UTC) + timedelta(days=1))

    body = world.client.get(f"/courses/{COURSE}").text
    assert "公開されている課題がありません" in body
    assert "学生には未公開" not in body


def test_staff_are_told_they_see_more_than_learners(world: World) -> None:
    """教員・TA の一覧には「教員・TA として表示」と出て、未公開のセットは枠で囲まれる。
    公開済みのセットは囲まない（誤って公開したら、枠が消えることで気づける）。"""
    world.register("teacher", role=Role.INSTRUCTOR)
    world.login("teacher")

    open_now = world.client.get(f"/courses/{COURSE}").text
    assert "教員・TA として表示しています" in open_now
    assert 'class="unit staff-only"' not in open_now

    tomorrow = datetime.now(UTC) + timedelta(days=1)
    _set_task(world, opens_at=tomorrow, submissions_open_at=tomorrow)
    body = world.client.get(f"/courses/{COURSE}").text
    assert 'class="unit staff-only"' in body
    assert (
        "学生にはまだ公開されていません" in world.client.get(f"/tasks/{world.task_version.id}").text
    )


def test_learners_get_no_staff_marks(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    body = world.client.get(f"/courses/{COURSE}").text
    assert "教員・TA として表示しています" not in body


def test_the_theme_switch_is_also_in_the_header(world: World) -> None:
    """昼夜の切り替えは上にも出す（フッタは長い画面では見えない）。フッタにも残る。"""
    world.register("s2400001")
    world.login("s2400001")
    body = world.client.get(f"/courses/{COURSE}").text
    assert "theme-switch js-only in-header" in body
    assert body.count('data-theme-choice="dark"') == 2
