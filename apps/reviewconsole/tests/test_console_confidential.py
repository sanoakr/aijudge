"""公開前の試験は、コンソールでも TA に見せない（`Task.confidential_until_open`）。

公開前の試験に教員が試しに出した提出は、模範解答に近い。TA がコンソールの
一覧やレビュー画面から読めるなら、学生画面で隠しても漏れる。

固定したいのは 4 つ。

レビュー画面   `/review/…` は TA に 404（`_load` の 1 か所で止まる）。
一覧           提出の一覧・問題セットの一覧に出ない。課題 ID を URL に載せても出ない。
公開で戻る     公開時刻を過ぎれば、TA にも見える。
教員と課題     教員には見える。秘匿でない課題は TA にも従来どおり見える。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_console import COURSE, World, _instructor_and_submission
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role

# 問題セットの一覧に出るのは課題名ではなくセットの名前（`unit`）。
UNIT = "exam01"


def _set(world: World, *, opens_in: timedelta, confidential: bool) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(
            task.model_copy(
                update={
                    "unit": UNIT,
                    "opens_at": datetime.now(UTC) + opens_in,
                    "confidential_until_open": confidential,
                }
            )
        )
        uow.commit()


def _graded_submission_seen_by_assistant(world: World):
    """教員が試しに出し、採点まで済んだ提出を、TA が開こうとしている状態。"""
    _instructor, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")
    return accepted.submission.id


def test_an_assistant_cannot_open_the_review_of_a_confidential_task(world: World) -> None:
    submission_id = _graded_submission_seen_by_assistant(world)
    _set(world, opens_in=timedelta(days=1), confidential=True)

    for path in ("reveal", "blind"):
        response = world.client.get(f"/review/{submission_id}/{path}", follow_redirects=False)
        assert response.status_code == 404, path


def test_the_listing_hides_it_even_when_asked_by_task_id(world: World) -> None:
    submission_id = _graded_submission_seen_by_assistant(world)
    _set(world, opens_in=timedelta(days=1), confidential=True)

    plain = world.client.get(f"/courses/{COURSE}/submissions").text
    by_task = world.client.get(
        f"/courses/{COURSE}/submissions?task={world.task_version.task_id}"
    ).text

    assert str(submission_id) not in plain
    assert str(submission_id) not in by_task


def test_the_course_menu_does_not_list_the_set_for_an_assistant(world: World) -> None:
    _graded_submission_seen_by_assistant(world)
    _set(world, opens_in=timedelta(days=1), confidential=True)

    body = world.client.get(f"/courses/{COURSE}").text

    assert UNIT not in body


def test_an_assistant_sees_it_once_it_opens(world: World) -> None:
    submission_id = _graded_submission_seen_by_assistant(world)
    _set(world, opens_in=-timedelta(minutes=1), confidential=True)

    assert world.client.get(f"/review/{submission_id}/reveal").status_code == 200
    assert str(submission_id) in world.client.get(f"/courses/{COURSE}/submissions").text


def test_a_plain_task_is_still_open_to_an_assistant_before_it_opens(world: World) -> None:
    """秘匿でない課題は従来どおり（#102）── 公開前でも TA が読める。"""
    submission_id = _graded_submission_seen_by_assistant(world)
    _set(world, opens_in=timedelta(days=1), confidential=False)

    assert world.client.get(f"/review/{submission_id}/reveal").status_code == 200


def test_an_instructor_still_sees_it(world: World) -> None:
    _instructor, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    _set(world, opens_in=timedelta(days=1), confidential=True)

    assert world.client.get(f"/review/{accepted.submission.id}/reveal").status_code == 200
    assert UNIT in world.client.get(f"/courses/{COURSE}").text
