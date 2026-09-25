"""提出の回数は課題の中で版をまたいで数える（`aijudge_core.ordinals`）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core import attempt_ordinals
from aijudge_core.ids import SubmissionId, TaskId, UserId

T0 = datetime(2026, 9, 18, tzinfo=UTC)
A = UserId("usr_" + "a" * 32)
B = UserId("usr_" + "b" * 32)
TASK = TaskId("tsk_" + "1" * 32)
OTHER = TaskId("tsk_" + "2" * 32)


def _sid(n: int) -> SubmissionId:
    return SubmissionId(f"sub_{n:032d}")


def test_attempts_restart_per_version_but_count_through_the_task() -> None:
    """9/18 に v2 へ 1 回目、9/23 に v5 へ 1 回目（記録上）→ 画面では 1 回目・2 回目。"""
    rows = [
        (_sid(2), A, TASK, T0 + timedelta(days=5), 1),
        (_sid(1), A, TASK, T0, 1),
    ]
    assert attempt_ordinals(rows) == {_sid(1): 1, _sid(2): 2}


def test_learners_and_tasks_are_counted_apart() -> None:
    rows = [
        (_sid(1), A, TASK, T0, 1),
        (_sid(2), B, TASK, T0 + timedelta(hours=1), 1),
        (_sid(3), A, OTHER, T0 + timedelta(hours=2), 1),
        (_sid(4), A, TASK, T0 + timedelta(hours=3), 2),
    ]
    assert attempt_ordinals(rows) == {_sid(1): 1, _sid(2): 1, _sid(3): 1, _sid(4): 2}
