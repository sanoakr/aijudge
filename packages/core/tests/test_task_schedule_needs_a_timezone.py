"""課題の予定の時刻はタイムゾーン付きに限る（#401）。

素の値が入ると、提出時刻（UTC）との比較が TypeError になり、その課題の
採点が全件失敗する。遅延減点の計算（`late_penalty_for`）でそれが起きた。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aijudge_core import Task
from aijudge_core.ids import CourseId, TaskId

FIELDS = ("opens_at", "submissions_open_at", "due_at", "grading_starts_at", "accepts_until")


def _task(**schedule: datetime) -> Task:
    return Task(
        id=TaskId("tsk_" + "1" * 32),
        course_id=CourseId("crs_" + "2" * 32),
        title="ex01 p1",
        **schedule,
    )


@pytest.mark.parametrize("field", FIELDS)
def test_a_naive_time_is_refused(field: str) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _task(**{field: datetime(2026, 9, 25, 23, 59)})


@pytest.mark.parametrize("field", FIELDS)
def test_an_aware_time_is_kept(field: str) -> None:
    at = datetime(2026, 9, 25, 14, 59, tzinfo=UTC)
    assert getattr(_task(**{field: at}), field) == at


def test_a_stored_task_still_loads() -> None:
    """保存済みの文書（ISO 8601 のオフセット付き）はそのまま読める。"""
    task = Task.model_validate(
        {
            "id": "tsk_" + "1" * 32,
            "course_id": "crs_" + "2" * 32,
            "title": "ex01 p1",
            "due_at": "2026-10-22T12:00:00+09:00",
        }
    )
    assert task.due_at is not None and task.due_at.utcoffset() is not None
