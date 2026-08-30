"""成績の確定。課題単位の一括確定と、締切経過による自動確定。

**なぜ確定の導線が要るか。** 教員の待ち行列は学習者からの異議申立だけに
なっている（ADR 0009）。それは正しい ── 91 名 × 十数課題を全件並べても
何から見ればよいか分からない ── が、そのままでは**依頼が出なかった提出が
永久に未確定で残る**。学期末に成績が閉じない。

**なぜ 2 つあるか。** 自動確定だけでは、レビュー方針が「人が見るべき」と
判定した提出（コンパイルエラー、合否境界の近傍）が残り続ける。一括確定
だけでは、教員が毎課題ごとに操作しないと成績が閉じない。両方要る。

**なぜ `HumanReview` を作らないか。** 誰も読んでいないのに「教員が AI の
判定に同意した」と記録すると、そこから測る一致度が嘘になる（ADR 0005）。
ここが作るのは `Finalization` だけで、測定側から見た状態は「まだ教員が
読んでいない」のまま変わらない。それが実態である。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aijudge_core import (
    DEADLINE_JUSTIFICATION,
    Course,
    Finalization,
    FinalizationSource,
    Task,
    auto_finalizable,
    blocks_finalization,
    bulk_finalizable,
    deadline_for,
    grace_minutes,
    new_id,
)
from aijudge_core.ids import CourseId, FinalizationId, TaskId, TenantId, UserId
from aijudge_persistence import Database
from aijudge_submission import ReviewRepository

from .operations import AdminError


@dataclass(frozen=True)
class TaskOutcome:
    """課題 1 件分の結果。**見送った件数と理由を必ず返す。**

    「12 件確定しました」だけでは、残った 3 件が何なのか運用者に分からない。
    自動確定は毎日走るので、そこで黙って積み上がるものが見えないと困る。
    """

    task: Task
    finalized: int = 0
    # 未対応の異議申立があるので見送った。教員が 1 件ずつ読むもの。
    contested: int = 0
    # レビュー方針が人の目を求めているので自動確定を見送った。
    needs_review: int = 0
    # 未採点の観点があるので見送った。誰も見ていない観点を成績にしない。
    provisional: int = 0

    @property
    def skipped(self) -> int:
        return self.contested + self.needs_review + self.provisional


@dataclass
class FinalizeReport:
    outcomes: list[TaskOutcome] = field(default_factory=list)

    @property
    def finalized(self) -> int:
        return sum(outcome.finalized for outcome in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(outcome.skipped for outcome in self.outcomes)

    @property
    def touched(self) -> tuple[TaskOutcome, ...]:
        """何かが起きた課題だけ。ログを静かに保つため。"""
        return tuple(o for o in self.outcomes if o.finalized or o.skipped)


def finalize_task(
    database: Database,
    *,
    task_id: TaskId,
    actor_id: UserId,
    justification: str,
    now: datetime | None = None,
    dry_run: bool = False,
) -> TaskOutcome:
    """課題の未確定分を教員の責任でまとめて確定する。

    `review_required` も含める ── 教員が根拠説明を書いて明示的に責任を
    取る操作なので、自動確定と同じ制限は課さない。**未対応の異議申立だけは
    残す**（そこは 1 件ずつ読むべきものとして待ち行列に置いておく）。
    """
    at = now or datetime.now(UTC)
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        if task is None:
            raise AdminError(f"課題 {task_id} がありません")
        outcome = _apply(
            uow.reviews,
            task,
            source=FinalizationSource.INSTRUCTOR_BULK,
            actor_id=actor_id,
            justification=justification,
            at=at,
        )
        if not dry_run:
            uow.commit()
    return outcome


def sweep_deadlines(
    database: Database,
    *,
    now: datetime | None = None,
    course_id: CourseId | None = None,
    dry_run: bool = False,
) -> FinalizeReport:
    """締切から所定の時間が過ぎた課題を自動確定する。

    猶予は**問題セットの指定がコースの既定を上書きする**（`grace_minutes`）。
    どちらも None なら飛ばす（自動確定しないという設定であって、設定漏れでは
    ない ── 既定は None で、教員が明示的に入れて初めて自動確定が始まる）。
    締切の無い課題も飛ばす。

    **何度走らせても同じ結果になる。** 確定済みの採点は
    `unfinalized_for_task` が返さず、返ってきても保存が拒否される。
    """
    at = now or datetime.now(UTC)
    report = FinalizeReport()
    for course in _courses(database, course_id):
        with database.unit_of_work() as uow:
            for task in uow.tasks.list_for_course(course.id):
                grace = grace_minutes(
                    task.auto_finalize_after_minutes, course.auto_finalize_after_minutes
                )
                cutoff = deadline_for(task.due_at, grace)
                if cutoff is None or at < cutoff:
                    continue
                report.outcomes.append(
                    _apply(
                        uow.reviews,
                        task,
                        source=FinalizationSource.DEADLINE_ELAPSED,
                        actor_id=None,
                        justification=DEADLINE_JUSTIFICATION,
                        at=at,
                    )
                )
            if not dry_run:
                uow.commit()
    return report


def finalize_tasks(
    database: Database,
    *,
    task_ids: Sequence[TaskId],
    actor_id: UserId,
    justification: str,
    now: datetime | None = None,
) -> tuple[TaskOutcome, ...]:
    """複数の課題をまとめて確定する。問題セット単位の確定がこれを使う。

    **1 つのトランザクションで閉じる。** 途中で落ちたときに半分だけ確定
    しているのは、教員から見て何が起きたのか分からない状態である。
    """
    at = now or datetime.now(UTC)
    outcomes: list[TaskOutcome] = []
    with database.unit_of_work() as uow:
        for task_id in task_ids:
            task = uow.tasks.get_task(task_id)
            if task is None:
                raise AdminError(f"課題 {task_id} がありません")
            outcomes.append(
                _apply(
                    uow.reviews,
                    task,
                    source=FinalizationSource.INSTRUCTOR_BULK,
                    actor_id=actor_id,
                    justification=justification,
                    at=at,
                )
            )
        uow.commit()
    return tuple(outcomes)


def pending_counts(database: Database, course_id: CourseId) -> dict[TaskId, int]:
    """課題ごとの未確定件数。管理画面が「あと何件残っているか」を出すのに使う。

    自動確定を設定したつもりで cron を仕掛け忘れると、件数が減らないことで
    気づける。画面に出ていなければ気づけない。
    """
    counts: dict[TaskId, int] = {}
    with database.unit_of_work() as uow:
        for task in uow.tasks.list_for_course(course_id):
            rows = uow.reviews.unfinalized_for_task(task.id)
            counts[task.id] = len(rows)
    return counts


def _apply(
    reviews: ReviewRepository,
    task: Task,
    *,
    source: FinalizationSource,
    actor_id: UserId | None,
    justification: str,
    at: datetime,
) -> TaskOutcome:
    automatic = source is FinalizationSource.DEADLINE_ELAPSED
    finalized = contested = needs_review = provisional = 0

    for _submission, run, request in reviews.unfinalized_for_task(task.id):
        if blocks_finalization(request):
            contested += 1
            continue
        if automatic and not auto_finalizable(run, request):
            # 何で見送ったのかを分けて数える。まとめると、運用者は
            # 「人が見るべきもの」と「採点が壊れたもの」を区別できない。
            if run.is_provisional:
                provisional += 1
            else:
                needs_review += 1
            continue
        if not automatic and not bulk_finalizable(run, request):  # pragma: no cover - 上で弾く
            contested += 1
            continue

        reviews.save_finalization(
            Finalization(
                id=FinalizationId(new_id("fin")),
                grading_run_id=run.id,
                source=source,
                actor_id=actor_id,
                review_id=None,
                justification=justification,
                finalized_at=at,
            )
        )
        finalized += 1

    return TaskOutcome(
        task=task,
        finalized=finalized,
        contested=contested,
        needs_review=needs_review,
        provisional=provisional,
    )


def _courses(database: Database, course_id: CourseId | None) -> tuple[Course, ...]:
    """対象のコース。指定が無ければ全テナントの全コース。

    自動確定は運用者が cron で回すもので、テナントを 1 つずつ挙げさせると
    テナントを足したときに漏れる。
    """
    from sqlalchemy import select

    from aijudge_persistence.schema import CourseRow

    with database.session() as session:
        statement = select(CourseRow).order_by(CourseRow.term, CourseRow.code)
        if course_id is not None:
            statement = statement.where(CourseRow.id == str(course_id))
        return tuple(
            Course(
                id=CourseId(row.id),
                tenant_id=TenantId(row.tenant_id),
                code=row.code,
                title=row.title,
                term=row.term,
                subject_profile=row.subject_profile,
                description=row.description,
                grading_overrides=dict(row.grading_overrides or {}),
                rubric=tuple(row.rubric or ()),
                auto_finalize_after_minutes=row.auto_finalize_after_minutes,
                upload_suffixes=tuple(row.upload_suffixes or ()),
            )
            for row in session.execute(statement).scalars()
        )


__all__ = [
    "FinalizeReport",
    "TaskOutcome",
    "finalize_task",
    "pending_counts",
    "sweep_deadlines",
]