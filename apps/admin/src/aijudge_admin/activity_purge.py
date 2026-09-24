"""保存期間を過ぎた IDE の行動記録と自動保存を消す（ADR 0023 §5）。

**保存期間は動画と同じ**（ADR 0020）── 締切があればそこから 6 ヶ月、無ければ
1 年。期限の計算はコアの `video_retention_expires_at` をそのまま使う（期間を
2 か所に書くと、片方だけ変わる）。

- **行動記録**（セッション単位）: 起点はそのセッションの問題セットの締切
  （セット内で最も遅いもの）。締切が無ければ、IDE を開いた時刻から 1 年
- **自動保存**（学習者 × 課題）: **受付終了から 1 ヶ月**（2026-09-24 決定）。受付終了
  の無い課題は最後に保存した時刻から 1 年。これも学習者のコードで、受付終了時の
  自動提出が済めば提出の複製でしかない（`aijudge_core.autosave_expires_at`）

提出の出どころの記録（`ide_submission_links`）は消さない。中身はコードではなく
指紋で、提出と同じだけ残す（提出が消えるときに一緒に消える）。

**2 段になっているのは、途中で落ちても続けられるようにするため**（動画と同じ）。
先に本体（ファイル）を消し、消せたセッションの索引だけを消す。逆にすると、索引が
無くなって本体を辿れないファイルが残る。

**自動では走らせない。** 呼ぶのは `aijudge-admin activity purge` だけで、既定は
下見である（`video purge` と同じ判断・#194）。
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import Course, Task, autosave_expires_at, video_retention_expires_at
from aijudge_core.ids import CourseId, TaskId, TenantId, UserId
from aijudge_ide import ActivityFiles, IdeSession
from aijudge_persistence import Database

from .operations import AdminError

__all__ = [
    "ActivityPurgeOutcome",
    "ActivityPurgePlan",
    "plan_activity_purge",
    "purge_activity",
]


@dataclass(frozen=True)
class ActivityPurgePlan:
    """何を消すことになるか。**消す前に人へ見せるためのもの。**"""

    now: datetime
    sessions: tuple[IdeSession, ...] = ()
    #: 消す自動保存 `(学習者, 課題)`。
    buffers: tuple[tuple[UserId, TaskId], ...] = ()
    #: 行動記録のうち、締切が無いので開いた時刻から数えたもの。
    sessions_without_deadline: int = 0
    #: まだ保存期間の中にあるもののうち、最も早く期限が来る時刻。
    next_expires_at: datetime | None = None
    #: 表示用: `(コース/問題セット, セッション数)`。
    by_unit: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ActivityPurgeOutcome:
    sessions: int = 0
    buffers: int = 0
    #: 本体を消せなかったセッション（ストレージの障害）。**索引は残す。**
    failed: tuple[str, ...] = field(default=())


def _unit_deadlines(tasks: Sequence[Task]) -> dict[str | None, datetime | None]:
    """問題セットごとの締切（セット内で最も遅いもの）。一つも無ければ None。"""
    deadlines: dict[str | None, datetime | None] = {}
    for task in tasks:
        current = deadlines.get(task.unit)
        if task.due_at is not None and (current is None or task.due_at > current):
            deadlines[task.unit] = task.due_at
        else:
            deadlines.setdefault(task.unit, current)
    return deadlines


def plan_activity_purge(
    database: Database,
    *,
    tenant_id: TenantId,
    now: datetime,
    course_id: CourseId | None = None,
) -> ActivityPurgePlan:
    """保存期間を過ぎた行動記録と自動保存を集める。**何も消さない。**"""
    sessions: list[IdeSession] = []
    buffers: list[tuple[UserId, TaskId]] = []
    without_deadline = 0
    next_expires_at: datetime | None = None
    counts: dict[str, int] = {}

    def later(expires_at: datetime) -> None:
        nonlocal next_expires_at
        if next_expires_at is None or expires_at < next_expires_at:
            next_expires_at = expires_at

    with database.unit_of_work() as uow:
        courses: Sequence[Course]
        if course_id is None:
            courses = uow.identity.list_courses(tenant_id)
        else:
            course = uow.identity.get_course(course_id)
            if course is None:
                raise AdminError(f"コース {course_id!r} がありません")
            if course.tenant_id != tenant_id:
                raise AdminError("このテナントのコースではありません")
            courses = (course,)

        for course in courses:
            tasks = uow.tasks.list_for_course(course.id)
            deadlines = _unit_deadlines(tasks)
            for session in uow.ide_activity.course_sessions(course.id):
                due = deadlines.get(session.unit)
                expires_at = video_retention_expires_at(due, submitted_at=session.started_at)
                if expires_at is None:  # pragma: no cover - 開いた時刻は必ずある
                    continue
                if now < expires_at:
                    later(expires_at)
                    continue
                if due is None:
                    without_deadline += 1
                sessions.append(session)
                label = f"{course.code} / {session.unit or '未分類'}"
                counts[label] = counts.get(label, 0) + 1
            for task in tasks:
                for buffer in uow.ide_buffers.for_task(task.id):
                    # 自動保存は**受付終了から 1 ヶ月**（コアの `autosave_expires_at`）。
                    # 終了時点の最新は自動提出で提出になっており、残すのは複製である。
                    expires_at = autosave_expires_at(
                        task.accepts_until, updated_at=buffer.updated_at
                    )
                    if now < expires_at:
                        later(expires_at)
                        continue
                    buffers.append((buffer.learner_id, buffer.task_id))

    return ActivityPurgePlan(
        now=now,
        sessions=tuple(sessions),
        buffers=tuple(buffers),
        sessions_without_deadline=without_deadline,
        next_expires_at=next_expires_at,
        by_unit=tuple(sorted(counts.items())),
    )


def purge_activity(
    database: Database,
    plan: ActivityPurgePlan,
    *,
    activity_dir: Path | None,
    tenant_id: TenantId,
    actor_id: UserId | None = None,
) -> ActivityPurgeOutcome:
    """下見で挙がったものを実際に消す。**本体を先に、索引をあとに。**

    `activity_dir` が無ければ行動記録は消さない（本体を消せないのに索引だけ消すと、
    誰にも辿れないファイルが残る）。自動保存は DB だけなので消す。
    """
    removable: list[IdeSession] = []
    failed: list[str] = []
    if activity_dir is not None:
        files = ActivityFiles(activity_dir)
        for session in plan.sessions:
            target = files.session_dir(session)
            try:
                if target.exists():
                    shutil.rmtree(target)
            except OSError:
                failed.append(str(session.id))
                continue
            removable.append(session)
    elif plan.sessions:
        failed.extend(str(session.id) for session in plan.sessions)

    with database.unit_of_work() as uow:
        removed = uow.ide_activity.delete_sessions([session.id for session in removable])
        for learner_id, task_id in plan.buffers:
            uow.ide_buffers.delete(learner_id, task_id)
        if removed or plan.buffers:
            # **消した事実を残す**（ADR 0016）。学習者のデータそのものは入れない
            # ── 入れるのは件数だけ。
            recorder = (
                AuditRecorder.for_user(uow.audit, tenant_id=tenant_id, user_id=actor_id)
                if actor_id is not None
                else AuditRecorder.for_system(uow.audit, tenant_id=tenant_id)
            )
            recorder.record(
                AuditAction.ACTIVITY_PURGED,
                target_type="activity",
                target_id=f"activity:{plan.now.date().isoformat()}",
                summary=(
                    f"保存期間を過ぎた作業の記録を {removed} 件、"
                    f"自動保存を {len(plan.buffers)} 件消しました"
                ),
                detail={
                    "sessions": removed,
                    "buffers": len(plan.buffers),
                    "failed": len(failed),
                },
            )
        uow.commit()
    return ActivityPurgeOutcome(sessions=removed, buffers=len(plan.buffers), failed=tuple(failed))
