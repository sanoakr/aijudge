"""得点の分布（`aijudge_reviewconsole.submissions.distribution_for`）。

**図は一覧とは別の読み出しから作る**（#253）。一覧に上限が要るのは描くから
で、図に上限は要らない ── 数えるだけなので件数に依らない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core import Role
from aijudge_core.ids import SubmissionId, TaskId, UserId

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)

TASK = TaskId("tsk_" + "2" * 32)


def test_the_distribution_counts_past_the_listing_limit() -> None:
    """**図は一覧の上限に縛られない**（#253）。

    一覧に上限が要るのは描くからで、図に上限は要らない ── 数えるだけなので
    件数に依らない。同じ行から作っていたころは、切られた分布がコース全体の
    分布として読まれた（#233）。頁送りが入れば 1 頁ぶんの分布になる（#255）。
    """
    from aijudge_reviewconsole.submissions import LISTING_LIMIT, Filters, distribution_for
    from aijudge_submission.protocols import ScoredRow

    rows = tuple(
        ScoredRow(
            submission_id=SubmissionId(f"sub_{index:032d}"),
            learner_id=UserId(f"usr_{index:032d}"),
            task_id=TASK,
            final_ratio=1.0,
            is_trial=False,
            submitted_at=NOW + timedelta(minutes=index),
            attempt=1,
            graded=True,
        )
        for index in range(LISTING_LIMIT + 10)
    )
    chart = distribution_for(rows, Filters())

    assert chart.scored == LISTING_LIMIT + 10
    assert chart.counts[-1] == LISTING_LIMIT + 10


def test_the_distribution_never_counts_a_trial() -> None:
    """教員・TA 自身の試行は到達度ではない（#108）。"""
    from aijudge_reviewconsole.submissions import Filters, distribution_for
    from aijudge_submission.protocols import ScoredRow

    def row(index: int, *, trial: bool) -> ScoredRow:
        return ScoredRow(
            submission_id=SubmissionId(f"sub_{index:032d}"),
            learner_id=UserId(f"usr_{index:032d}"),
            task_id=TASK,
            final_ratio=1.0,
            is_trial=trial,
            submitted_at=NOW + timedelta(minutes=index),
            attempt=1,
            graded=True,
        )

    chart = distribution_for((row(1, trial=False), row(2, trial=True)), Filters())
    assert chart.total == 1
    assert chart.scored == 1


def test_a_role_other_than_learner_leaves_the_distribution_empty() -> None:
    """**役割は数える前に決着する**（#253）。

    図は試行を数えない（#108）。`is_trial` は「学習者以外が出した、または
    デモ」なので、学習者以外の役割で絞れば母数は必ず空になる ── 近似では
    なく `Submission.is_trial` の定義からそうなる。
    """
    from aijudge_reviewconsole.submissions import Filters, distribution_for
    from aijudge_submission.protocols import ScoredRow

    rows = (
        ScoredRow(
            submission_id=SubmissionId("sub_" + "1" * 32),
            learner_id=UserId("usr_" + "1" * 32),
            task_id=TASK,
            final_ratio=1.0,
            is_trial=False,
            submitted_at=NOW,
            attempt=1,
            graded=True,
        ),
    )
    assert distribution_for(rows, Filters(role=Role.LEARNER.value)).scored == 1
    assert distribution_for(rows, Filters(role=Role.INSTRUCTOR.value)).scored == 0
