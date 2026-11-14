"""締切・受付終了・遅延減点の段の「ちょうど」を固定する（#684）。

境目ちょうどの提出は珍しくない。受付終了の自動提出（`Submission.auto_closed`）は
`accepts_until` の時刻で出るし、締切の秒に押された提出もある。ここで `<` と `<=`
が入れ替わっても、スイートは通っていた。

揃っている必要がある 2 か所。

- `Task.submission_window_at` の「締切を過ぎたら LATE」と、`late_penalty_for` の
  「締切ちょうどまでは減点しない」。食い違うと、画面では減点提出と出たのに減点が
  付かない（またはその逆）。
- `Task.accepts_submissions_at` と `submission_window_at` の受付終了。食い違うと、
  一覧では出せると表示した提出が受付で弾かれる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import LatePenaltyStep, SubmissionWindow, Task, late_penalty_for
from aijudge_core.ids import CourseId, TaskId

DUE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ACCEPTS_UNTIL = DUE + timedelta(hours=24)
ONE_SECOND = timedelta(seconds=1)
LADDER = (
    LatePenaltyStep(after_hours=0.0, ratio=0.10),
    LatePenaltyStep(after_hours=24.0, ratio=0.30),
    LatePenaltyStep(after_hours=72.0, ratio=0.50),
)


def _task() -> Task:
    return Task(
        id=TaskId("tsk_" + "0" * 32),
        course_id=CourseId("crs_" + "0" * 32),
        title="課題",
        due_at=DUE,
        accepts_until=ACCEPTS_UNTIL,
    )


# --------------------------------------------------------------------------
# 提出窓
# --------------------------------------------------------------------------


def test_a_submission_at_the_deadline_is_on_time() -> None:
    """締切の時刻ちょうどは締切内。減点の側（`late_penalty_for`）と同じ向き。"""
    task = _task()
    assert task.submission_window_at(DUE) is SubmissionWindow.OPEN
    assert late_penalty_for(DUE, DUE, LADDER) is None

    assert task.submission_window_at(DUE + ONE_SECOND) is SubmissionWindow.LATE
    assert late_penalty_for(DUE, DUE + ONE_SECOND, LADDER) is not None


def test_the_end_of_acceptance_is_still_accepted() -> None:
    """受付終了の時刻ちょうどは受け付ける ── 自動提出はこの時刻で出る。"""
    task = _task()
    assert task.submission_window_at(ACCEPTS_UNTIL) is SubmissionWindow.LATE
    assert task.accepts_submissions_at(ACCEPTS_UNTIL)

    assert task.submission_window_at(ACCEPTS_UNTIL + ONE_SECOND) is SubmissionWindow.CLOSED
    assert not task.accepts_submissions_at(ACCEPTS_UNTIL + ONE_SECOND)


# --------------------------------------------------------------------------
# 遅延減点の段
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(24.0, 0.10), (72.0, 0.30)],
)
def test_a_delay_exactly_on_a_step_stays_on_the_lower_step(hours: float, expected: float) -> None:
    """段は「この時間を**超えたら**」。ちょうど 24 時間後の提出は 10% のまま。"""
    penalty = late_penalty_for(DUE, DUE + timedelta(hours=hours), LADDER)
    assert penalty is not None
    assert penalty.ratio == expected


def test_a_zero_step_records_no_penalty() -> None:
    """比率 0 の段（「24 時間までは減点なし」を明示した梯子）では減点を記録しない。

    0% の `LatePenalty` を残すと、画面に中身の無い「遅延の減点」欄が出る。
    設定画面からは作れない段（`0 < percent`）だが、YAML からは入る。
    """
    ladder = (
        LatePenaltyStep(after_hours=0.0, ratio=0.0),
        LatePenaltyStep(after_hours=24.0, ratio=0.30),
    )
    assert late_penalty_for(DUE, DUE + timedelta(hours=12), ladder) is None

    later = late_penalty_for(DUE, DUE + timedelta(hours=30), ladder)
    assert later is not None
    assert later.ratio == 0.30
