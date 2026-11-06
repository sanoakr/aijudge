"""教員の確認後に結果を返す問題セット（ADR 0033）。

固定したいのは 4 つ。

持たせない    確認の前は、結果の値を `ResultView` が持たない（隠すのではない）
出さない      結果の画面・課題の画面に、得点も判定も出ない。依頼も出せない
確認で返る    `HumanReview` が付くと、ふつうの結果として返る
既定は従来    設定しない問題セットの見え方は変わらない
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_failed_inputs import _run as _run_failing
from test_failed_inputs import _version as _task_version
from test_studentweb import World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import (
    Finalization,
    FinalizationSource,
    HumanReview,
    new_id,
)
from aijudge_core.ids import FinalizationId, HumanReviewId, SubmissionId, UserId
from aijudge_studentweb.visibility import build_result_view

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
GRADER = UserId("usr_" + "6" * 32)
WAITING = "担当教員が確認してから"


def _run():
    return _run_failing(set())


def _review(run) -> HumanReview:
    return HumanReview(
        id=HumanReviewId(new_id("hrv")),
        grading_run_id=run.id,
        grader_id=GRADER,
        comment="本文を読みました。考察の根拠は十分です。",
        reviewed_at=NOW,
    )


def _finalization(run) -> Finalization:
    return Finalization(
        id=FinalizationId(new_id("fin")),
        grading_run_id=run.id,
        source=FinalizationSource.INSTRUCTOR_BULK,
        actor_id=GRADER,
        review_id=None,
        justification="設定を入れる前にまとめて確定しました。",
        finalized_at=NOW,
    )


def test_a_held_view_has_no_result_to_show() -> None:
    run = _run()

    view = build_result_view(run, _task_version(), None, results_after_review=True)

    assert view.held_for_review
    assert view.criteria == ()
    assert view.score_ratio is None
    assert view.evaluation_ratio is None
    assert view.penalty is None
    assert view.feedback is None
    assert view.settles_at is None
    assert not view.can_request_review
    assert not view.score_withheld


def test_a_review_releases_the_result() -> None:
    run = _run()

    view = build_result_view(run, _task_version(), _review(run), results_after_review=True)

    assert not view.held_for_review
    assert view.criteria
    assert view.score_ratio is not None
    assert view.reviewed_individually


def test_a_grade_closed_before_the_setting_stays_visible() -> None:
    run = _run()

    view = build_result_view(
        run, _task_version(), None, finalization=_finalization(run), results_after_review=True
    )

    assert not view.held_for_review
    assert view.score_ratio is not None


def test_the_default_shows_the_result_at_once() -> None:
    view = build_result_view(_run(), _task_version(), None)

    assert not view.held_for_review
    assert view.score_ratio is not None


def _hold(world: World) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update={"results_after_review": True}))
        uow.commit()


def _graded_submission(world: World) -> tuple[str, SubmissionId]:
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_until_empty()
    return location, SubmissionId(location.split("?")[0].rstrip("/").rsplit("/", 1)[-1])


def test_a_unit_without_the_setting_shows_a_score(world: World) -> None:
    location, _id = _graded_submission(world)

    body = world.client.get(location).text

    assert 'class="score"' in body
    assert WAITING not in body


def test_the_result_page_says_it_is_waiting_and_shows_nothing_else(world: World) -> None:
    _hold(world)
    location, submission_id = _graded_submission(world)
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
    assert run is not None, "採点そのものは走っている"

    body = world.client.get(location).text

    assert WAITING in body
    assert 'class="score"' not in body
    assert 'data-poll="' not in body
    assert "再確認" not in body
    for score in run.criterion_scores:
        assert score.rationale not in body


def test_the_note_is_there_before_the_grading_finishes(world: World) -> None:
    _hold(world)
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]

    body = world.client.get(location).text

    assert WAITING in body
    assert 'data-poll="' not in body


def test_the_task_page_shows_no_score_while_waiting(world: World) -> None:
    _hold(world)
    _graded_submission(world)

    body = world.client.get(f"/tasks/{world.task_version.id}").text

    assert "教員の確認待ち" in body
    assert "採用" not in body


def test_a_review_request_is_refused_while_waiting(world: World) -> None:
    _hold(world)
    location, submission_id = _graded_submission(world)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "どの観点のどこが違うのかを、十分な長さで書きました。"},
    )

    assert response.status_code == 409, location


def test_the_instructor_review_returns_the_result(world: World) -> None:
    _hold(world)
    location, submission_id = _graded_submission(world)
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        uow.reviews.save_review(_review(run))
        uow.commit()

    body = world.client.get(location).text

    assert WAITING not in body
    assert 'class="score"' in body
