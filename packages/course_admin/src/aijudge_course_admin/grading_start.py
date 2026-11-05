"""採点開始時刻の変更を、待機中の提出に反映する（2026-11-05）。

ジョブは提出のときの開始時刻で寝かせる。変更を反映しないと、変更前の提出だけが
古い時刻まで待ち続ける（prog2 ex07 で 17:30 を 17:00 に早めたときに、そうなった）。
**開始時刻を書き換える経路は、保存と同じ作業単位でここを呼ぶ。**
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import Task
from aijudge_unit_of_work import UnitOfWork


def follow_grading_start(
    uow: UnitOfWork, before: Task | None, after: Task, *, now: datetime | None = None
) -> int:
    """開始時刻が変わっていれば、待機中のジョブを合わせる。動かした件数を返す。"""
    if before is None or before.grading_starts_at == after.grading_starts_at:
        return 0
    versions = uow.tasks.list_versions(after.id)
    submissions = uow.submissions.list_for_versions([version.id for version in versions])
    return uow.jobs.reschedule_waiting(
        [submission.id for submission in submissions],
        after.grading_starts_at,
        now or datetime.now(UTC),
    )


__all__ = ["follow_grading_start"]
