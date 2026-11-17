"""確定済みの成績にも、決めた時刻まで再確認を依頼できる（2026-11-17）。

問題セットが `review_requests_until` を持つとき、その時刻までは確定済み（自動確定の
猶予が明けたものを含む）の成績にも学習者が依頼できる。**画面の可否と POST の可否は
同じ判定を通る** ── 押せない欄だけを信用しない。時刻を過ぎたら、設定が無いときと同じで、
従来どおり断る。

依頼のあとの流れ（教員の待ち行列・追記での訂正・学習者への表示）は、確定の記録を
消さず上書きもしない既存の経路（自動確定 → 異議 → 教員が読む）を使う（P8）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_studentweb import World, needs_c_compiler
from test_visibility import _run_missing_the_ai_criterion, _task_version

from aijudge_core import (
    Finalization,
    FinalizationSource,
    ReviewRequest,
    Routing,
    new_id,
)
from aijudge_core.ids import FinalizationId, ReviewRequestId, UserId
from aijudge_studentweb.visibility import build_result_view

NOW = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 17, 14, 59, tzinfo=UTC)
GRACE_MINUTES = 120


def _complete_run():
    """欠けの無い採点（点が定まっている）。"""
    return _run_missing_the_ai_criterion().model_copy(
        update={"unscored_criteria": (), "routing": Routing.AUTO, "created_at": NOW}
    )


def _auto_finalization(run) -> Finalization:
    return Finalization(
        id=FinalizationId(new_id("fin")),
        grading_run_id=run.id,
        source=FinalizationSource.AUTOMATIC,
        actor_id=None,
        review_id=None,
        justification="締切から所定の時間が経過したため、AI の判定のまま確定しました。",
        finalized_at=NOW + timedelta(hours=3),
    )


def _view(*, until, now, finalized=True, request=None, run=None):
    run = run or _complete_run()
    return build_result_view(
        run,
        _task_version(),
        None,
        finalization=_auto_finalization(run) if finalized else None,
        request=request,
        auto_finalize_after_minutes=GRACE_MINUTES,
        now=now,
        review_requests_until=until,
    )


def test_a_finalized_grade_can_be_contested_until_the_deadline() -> None:
    view = _view(until=UNTIL, now=UNTIL - timedelta(hours=1))

    assert view.can_request_review
    assert view.review_window_until == UNTIL


def test_after_the_deadline_a_finalized_grade_is_closed_again() -> None:
    view = _view(until=UNTIL, now=UNTIL + timedelta(seconds=1))

    assert not view.can_request_review
    assert view.review_window_until is None
    assert view.request_reason == "確定済みの成績です。申し出は担当教員に直接お願いします。"


def test_without_a_deadline_nothing_changes() -> None:
    view = _view(until=None, now=UNTIL - timedelta(hours=1))

    assert not view.can_request_review
    assert view.review_window_until is None


def test_a_grade_whose_auto_finalize_window_elapsed_can_be_contested_too() -> None:
    """確定の記録がまだ無くても、猶予が明けていれば本来は閉じている。"""
    view = _view(until=UNTIL, now=UNTIL - timedelta(hours=1), finalized=False)

    assert view.can_request_review


def test_an_unscored_grade_still_goes_to_the_instructor_not_the_form() -> None:
    run = _run_missing_the_ai_criterion()
    view = _view(until=UNTIL, now=UNTIL - timedelta(hours=1), run=run)

    assert not view.can_request_review


def test_a_second_request_is_refused_inside_the_window() -> None:
    run = _complete_run()
    request = ReviewRequest(
        id=ReviewRequestId(new_id("rrq")),
        submission_id=run.submission_id,
        grading_run_id=run.id,
        learner_id=UserId(new_id("usr")),
        reason="テストケース 3 の想定出力が違うと思います。",
        requested_at=NOW + timedelta(hours=4),
    )
    view = _view(until=UNTIL, now=UNTIL - timedelta(hours=1), request=request, run=run)

    assert not view.can_request_review
    assert view.request_reason == "再確認を依頼済みです。担当教員の対応をお待ちください。"


def _finalized_world(tmp_path, offset: timedelta | None):
    """自動確定まで済んだ提出と、`review_requests_until`（現在 + offset）を持つ世界。"""
    world = World(tmp_path)
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_until_empty()
    submission_id = location.split("/")[-1].split("?")[0]
    world.finalize(submission_id, source=FinalizationSource.AUTOMATIC)
    if offset is not None:
        with world.database.unit_of_work() as uow:
            task = uow.tasks.get_task(world.task_version.task_id)
            until = datetime.now(UTC) + offset
            uow.tasks.save_task(task.model_copy(update={"review_requests_until": until}))
            uow.commit()
    return world, location, submission_id


@needs_c_compiler
@pytest.mark.parametrize(
    ("offset", "expected_status"),
    [
        (timedelta(hours=1), 303),
        (timedelta(hours=-1), 409),
        (None, 409),
    ],
    ids=["inside the window", "after the deadline", "no deadline"],
)
def test_the_server_enforces_the_window(tmp_path, offset, expected_status) -> None:
    """**ボタンの有無ではなく、サーバーが断る。** 無効な欄に直接 POST しても通らない。"""
    world, location, submission_id = _finalized_world(tmp_path, offset)
    try:
        response = world.client.post(
            f"/submissions/{submission_id}/request-review",
            data={"reason": "自動で確定しましたが、テストケース 3 の想定出力が違うと思います。"},
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        with world.database.unit_of_work() as uow:
            run = uow.runs.latest_for(submission_id)
            stored = uow.reviews.find_request_for_run(run.id)
            finalization = uow.reviews.find_finalization_for_run(run.id)
        assert (stored is not None) == (expected_status == 303)
        # 確定の記録は残ったまま（追記のみ・P8）。依頼が増えるだけである。
        assert finalization is not None
        if expected_status == 303:
            assert "再確認を依頼済みです" in world.client.get(location).text
    finally:
        world.close()


@needs_c_compiler
def test_the_page_says_until_when_a_finalized_grade_can_be_contested(tmp_path) -> None:
    world, location, _ = _finalized_world(tmp_path, timedelta(hours=1))
    try:
        body = world.client.get(location).text

        assert "確定済みですが" in body
        assert "再確認を依頼できます" in body
        assert 'action="/submissions/' in body, "依頼の欄が開いていない"
    finally:
        world.close()
