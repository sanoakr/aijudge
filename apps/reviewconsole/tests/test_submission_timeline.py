"""提出の時刻（`aijudge_reviewconsole.submissions.timeline_for`・2026-10-22）。

固定したいのは 4 つ。

分布と同じ行   試行は数えない・採点待ちは数える（点の図ではない）
棒の幅       範囲が 2 日以内は 1 時間、62 日以内は 1 日、それより長ければ 1 週
表示の時刻     時刻帯も区切りも表示のタイムゾーン（既定は日本）で数える
締切         絞り込んだ回の締切が入る棒に印を付け、直前 24 時間と締切後を数える
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_console import COURSE, World, _instructor_and_submission, needs_c_compiler
from test_console import world as world  # フィクスチャを借りる

from aijudge_core.ids import SubmissionId, TaskId, UserId
from aijudge_reviewconsole.submissions import Filters, timeline_for
from aijudge_submission.protocols import ScoredRow

TASK = TaskId("tsk_" + "3" * 32)
# 2026-10-22 00:00 JST
START = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


def _row(index: int, at: datetime, *, trial: bool = False, graded: bool = True) -> ScoredRow:
    return ScoredRow(
        submission_id=SubmissionId(f"sub_{index:032d}"),
        learner_id=UserId(f"usr_{index:032d}"),
        task_id=TASK,
        final_ratio=1.0 if graded else None,
        is_trial=trial,
        submitted_at=at,
        attempt=1,
        graded=graded,
    )


def test_a_short_span_is_drawn_hour_by_hour_in_local_time() -> None:
    rows = (
        _row(1, START + timedelta(minutes=10)),
        _row(2, START + timedelta(minutes=50)),
        _row(3, START + timedelta(hours=3, minutes=5)),
    )
    chart = timeline_for(rows, Filters())

    assert chart.width == "1 時間"
    # 空の時間も棒として出す（0 時・1 時・2 時・3 時）── 詰めると間隔が読めない。
    assert [b.count for b in chart.bins] == [2, 0, 0, 1]
    assert chart.bins[0].start.hour == 0, "区切りは日本時間の 0 時"
    assert chart.hours[0] == 2 and chart.hours[3] == 1
    assert chart.total == 3


def test_the_width_grows_with_the_span() -> None:
    days = timeline_for((_row(1, START), _row(2, START + timedelta(days=6))), Filters())
    assert days.width == "1 日" and len(days.bins) == 7

    weeks = timeline_for((_row(1, START), _row(2, START + timedelta(days=120))), Filters())
    assert weeks.width == "1 週"
    assert all(b.start.weekday() == 0 for b in weeks.bins), "週は月曜から"
    assert sum(b.count for b in weeks.bins) == 2


def test_trials_are_left_out_but_ungraded_submissions_count() -> None:
    rows = (
        _row(1, START, trial=True),
        _row(2, START + timedelta(minutes=1), graded=False),
    )
    chart = timeline_for(rows, Filters())
    assert chart.total == 1 and chart.trials == 1

    demo = timeline_for(rows, Filters(), count_trials=True)
    assert demo.total == 2


def test_the_deadline_bin_is_marked_and_the_rush_counted() -> None:
    due = START + timedelta(days=3)
    rows = (
        _row(1, START),
        _row(2, due - timedelta(hours=2)),
        _row(3, due - timedelta(minutes=5)),
        _row(4, due + timedelta(hours=1)),
    )
    chart = timeline_for(rows, Filters(), due_at=due)

    marked = [b for b in chart.bins if b.has_due]
    assert len(marked) == 1 and marked[0].start <= due.astimezone(marked[0].start.tzinfo)
    assert chart.before_due == 2
    assert chart.after_due == 1


def test_nothing_to_draw_says_so() -> None:
    chart = timeline_for((_row(1, START, trial=True),), Filters())
    assert chart.bins == () and chart.trials == 1 and chart.total == 0


@needs_c_compiler
def test_the_submissions_page_shows_when_work_arrived(world: World) -> None:
    _instructor_and_submission(world)
    world.worker.run_until_empty()

    body = world.client.get(f"/courses/{COURSE}/submissions").text
    assert "提出の時刻" in body
    assert 'class="hist timeline"' in body
    assert 'class="hist hours"' in body


def test_the_two_days_before_the_deadline_are_drawn_hour_by_hour() -> None:
    """1 週間の演習は推移が 1 日ごとになるので、締切前 2 日間を 1 時間ごとに足す。"""
    due = START + timedelta(days=7, hours=12)  # 10-29 12:00 JST
    rows = (
        _row(1, START),
        _row(2, due - timedelta(hours=3, minutes=30)),
        _row(3, due - timedelta(minutes=20)),
        _row(4, due - timedelta(minutes=5)),
    )
    chart = timeline_for(rows, Filters(), due_at=due)

    assert chart.width == "1 日"
    assert len(chart.closeup) == 49, "48 時間ぶんと、締切を含む 1 本"
    assert chart.closeup[-1].has_due
    assert chart.closeup[-1].count == 0, "締切ちょうどの 12 時台には来ていない"
    assert chart.closeup[-2].count == 2, "11 時台に 2 件"
    assert sum(b.count for b in chart.closeup) == 3


def test_no_closeup_without_a_deadline_or_when_already_hourly() -> None:
    week = (_row(1, START), _row(2, START + timedelta(days=6)))
    assert timeline_for(week, Filters()).closeup == ()

    short = (_row(1, START), _row(2, START + timedelta(hours=5)))
    hourly = timeline_for(short, Filters(), due_at=START + timedelta(hours=6))
    assert hourly.width == "1 時間" and hourly.closeup == ()
