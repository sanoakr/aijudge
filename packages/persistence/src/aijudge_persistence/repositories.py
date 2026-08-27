"""S2 / S3 のプロトコルの SQLAlchemy 実装。

インメモリ実装と**同じ規則**を守る。ここで緩めると、テストが通るのに
本番で落ちる（あるいはその逆）。特に:

- `GradingRunRepository.save` は上書きを拒否する（P8）
- `supersede` は二度書き換えない
- `JobQueue.enqueue` は冪等キーで既存を返す
- `reserve` はリースの切れた RUNNING を取り直し、**行ロックで二重配布を防ぐ**

インメモリ実装との違いが 1 つある。同時実行である。アプリ側の
「あれば返す」だけでは、同時に来た 2 つのリクエストが両方「無い」を見て
両方作る。DB 側の一意制約と行ロックが最後の砦になる。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aijudge_authoring.repository import TaskImmutabilityViolation, TaskStoreError
from aijudge_core import BlindMark, GradingRun, HumanReview, Submission, Task, TaskVersion
from aijudge_core.events import EVENT_TYPES, DomainEvent
from aijudge_core.ids import (
    CourseId,
    GradingJobId,
    GradingRunId,
    HumanReviewId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_submission import GradingJob, JobState
from aijudge_submission.protocols import ImmutabilityViolation, SubmissionStoreError

from .schema import (
    BlindMarkRow,
    GradingJobRow,
    GradingRunRow,
    HumanReviewRow,
    OutboxRow,
    SubmissionKeyRow,
    SubmissionRow,
    TaskRow,
    TaskVersionRow,
)


def _dump(model: object) -> dict:
    return model.model_dump(mode="json")  # type: ignore[attr-defined]


class SqlSubmissionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, submission: Submission) -> None:
        existing = self._session.get(SubmissionRow, str(submission.id))
        if existing is not None:
            if existing.submitted_at is not None:
                raise ImmutabilityViolation(f"submission {submission.id} is already submitted")
            existing.state = submission.state.value
            existing.attempt = submission.attempt
            existing.submitted_at = submission.submitted_at
            existing.document = _dump(submission)
            return
        # tenant_id は Submission に無い（コアはテナントを持たない）。
        # 冪等キーの記録時に埋まる。ここでは空で入れ、下で更新する。
        self._session.add(
            SubmissionRow(
                id=str(submission.id),
                tenant_id="",
                task_version_id=str(submission.task_version_id),
                learner_id=str(submission.learner_id),
                state=submission.state.value,
                attempt=submission.attempt,
                created_at=submission.created_at,
                submitted_at=submission.submitted_at,
                document=_dump(submission),
            )
        )
        self._session.flush()

    def get(self, submission_id: SubmissionId) -> Submission | None:
        row = self._session.get(SubmissionRow, str(submission_id))
        return None if row is None else Submission.model_validate(row.document)

    def find_by_idempotency_key(self, tenant_id: TenantId, key: str) -> Submission | None:
        row = self._session.get(SubmissionKeyRow, (str(tenant_id), key))
        return None if row is None else self.get(SubmissionId(row.submission_id))

    def remember_idempotency_key(
        self, tenant_id: TenantId, key: str, submission_id: SubmissionId
    ) -> None:
        self._session.add(
            SubmissionKeyRow(
                tenant_id=str(tenant_id),
                idempotency_key=key,
                submission_id=str(submission_id),
            )
        )
        # テナントは提出行にも持たせる（テナント単位の走査に索引を使うため）。
        self._session.execute(
            update(SubmissionRow)
            .where(SubmissionRow.id == str(submission_id))
            .values(tenant_id=str(tenant_id))
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            # 同時に来た 2 つのリクエストが両方「無い」を見た場合。
            # 一意制約が止めるので、呼び出し側は再読み込みして既存を使う。
            raise ImmutabilityViolation(
                f"idempotency key {key!r} was claimed concurrently"
            ) from exc

    def list_for_learner(
        self,
        tenant_id: TenantId,
        learner_id: UserId,
        task_version_id: TaskVersionId | None = None,
    ) -> tuple[Submission, ...]:
        statement = (
            select(SubmissionRow)
            .where(
                SubmissionRow.tenant_id == str(tenant_id),
                SubmissionRow.learner_id == str(learner_id),
            )
            .order_by(SubmissionRow.created_at, SubmissionRow.id)
        )
        if task_version_id is not None:
            statement = statement.where(SubmissionRow.task_version_id == str(task_version_id))
        return tuple(
            Submission.model_validate(row.document)
            for row in self._session.execute(statement).scalars()
        )

    def next_attempt(
        self, tenant_id: TenantId, learner_id: UserId, task_version_id: TaskVersionId
    ) -> int:
        return len(self.list_for_learner(tenant_id, learner_id, task_version_id)) + 1


class SqlGradingRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, run: GradingRun) -> None:
        if self._session.get(GradingRunRow, str(run.id)) is not None:
            raise ImmutabilityViolation(
                f"GradingRun {run.id} already exists; re-grading creates a new run (P8)"
            )
        self._session.add(
            GradingRunRow(
                id=str(run.id),
                submission_id=str(run.submission_id),
                task_version_id=str(run.context.task_version_id),
                subject_profile=run.context.subject_profile,
                input_hash=run.context.input_hash,
                score_ratio=run.score_ratio,
                confidence=run.confidence,
                routing=run.routing.value,
                superseded_by=None if run.superseded_by is None else str(run.superseded_by),
                created_at=run.created_at,
                document=_dump(run),
            )
        )
        self._session.flush()

    def get(self, run_id: GradingRunId) -> GradingRun | None:
        row = self._session.get(GradingRunRow, str(run_id))
        return None if row is None else GradingRun.model_validate(row.document)

    def list_for(self, submission_id: SubmissionId) -> tuple[GradingRun, ...]:
        rows = self._session.execute(
            select(GradingRunRow)
            .where(GradingRunRow.submission_id == str(submission_id))
            .order_by(GradingRunRow.created_at, GradingRunRow.id)
        ).scalars()
        return tuple(GradingRun.model_validate(row.document) for row in rows)

    def latest_for(self, submission_id: SubmissionId) -> GradingRun | None:
        runs = self.list_for(submission_id)
        return runs[-1] if runs else None

    def supersede(self, old_id: GradingRunId, new_id: GradingRunId) -> None:
        row = self._session.get(GradingRunRow, str(old_id))
        if row is None:
            raise SubmissionStoreError(f"no GradingRun {old_id}")
        if row.superseded_by is not None:
            raise ImmutabilityViolation(
                f"GradingRun {old_id} is already superseded by {row.superseded_by}"
            )
        row.superseded_by = str(new_id)
        document = dict(row.document)
        document["superseded_by"] = str(new_id)
        row.document = document
        self._session.flush()


class SqlReviewRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_review(self, review: HumanReview) -> None:
        run = self._session.get(GradingRunRow, str(review.grading_run_id))
        if run is None:
            raise SubmissionStoreError(f"no GradingRun {review.grading_run_id}")
        row = self._session.get(HumanReviewRow, str(review.id))
        if row is None:
            existing = (
                self._session.execute(
                    select(HumanReviewRow).where(
                        HumanReviewRow.grading_run_id == str(review.grading_run_id)
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                # 二度確定できると成績が二つ存在する。やり直しは再採点から。
                raise ImmutabilityViolation(
                    f"GradingRun {review.grading_run_id} is already finalised by "
                    f"{existing.grader_id}"
                )
            self._session.add(
                HumanReviewRow(
                    id=str(review.id),
                    grading_run_id=str(review.grading_run_id),
                    submission_id=str(run.submission_id),
                    grader_id=str(review.grader_id),
                    agreed=review.agreed,
                    reviewed_at=review.reviewed_at,
                    document=_dump(review),
                )
            )
        else:
            raise ImmutabilityViolation(f"HumanReview {review.id} already exists")
        self._session.flush()

    def get_review(self, review_id: HumanReviewId) -> HumanReview | None:
        row = self._session.get(HumanReviewRow, str(review_id))
        return None if row is None else HumanReview.model_validate(row.document)

    def find_review_for_run(self, run_id: GradingRunId) -> HumanReview | None:
        row = (
            self._session.execute(
                select(HumanReviewRow).where(HumanReviewRow.grading_run_id == str(run_id))
            )
            .scalars()
            .first()
        )
        return None if row is None else HumanReview.model_validate(row.document)

    def save_blind_mark(self, mark: BlindMark) -> None:
        if self._session.get(BlindMarkRow, str(mark.submission_id)) is not None:
            raise ImmutabilityViolation(
                f"submission {mark.submission_id} already has a blind mark; "
                "overwriting it would let a post-AI grade become ground truth (ADR 0005)"
            )
        self._session.add(
            BlindMarkRow(
                submission_id=str(mark.submission_id),
                grader_id=str(mark.grader_id),
                marked_at=mark.marked_at,
                document=_dump(mark),
            )
        )
        self._session.flush()

    def find_blind_mark(self, submission_id: SubmissionId) -> BlindMark | None:
        row = self._session.get(BlindMarkRow, str(submission_id))
        return None if row is None else BlindMark.model_validate(row.document)

    # -- レビュー待ち行列（教員 UI 用の読み取り）--------------------------

    def pending_for_course(
        self, course_id: CourseId, *, include_decided: bool = False, limit: int = 200
    ) -> tuple[tuple[Submission, GradingRun], ...]:
        """このコースで教員の確認を待っている提出。

        「採点が届いていて、まだ確定していない」もの。採点が届いていない
        提出をレビュー画面に出さないのは、そこで採点を起動しないため
        （ADR 0007）。提出のたびに 1 行ではなく、**最新の採点 1 件につき 1 行**。

        課題 → コースの経路で絞る。提出は課題版を指しており、コースを
        直接持たない（持たせると課題の移動で片方だけ古くなる）。
        """
        latest = (
            select(
                GradingRunRow.submission_id.label("submission_id"),
                func.max(GradingRunRow.created_at).label("created_at"),
            )
            .group_by(GradingRunRow.submission_id)
            .subquery()
        )
        statement = (
            select(SubmissionRow, GradingRunRow)
            .join(
                TaskVersionRow,
                TaskVersionRow.id == SubmissionRow.task_version_id,
            )
            .join(TaskRow, TaskRow.id == TaskVersionRow.task_id)
            .join(GradingRunRow, GradingRunRow.submission_id == SubmissionRow.id)
            .join(
                latest,
                (latest.c.submission_id == GradingRunRow.submission_id)
                & (latest.c.created_at == GradingRunRow.created_at),
            )
            .where(TaskRow.course_id == str(course_id))
            .order_by(SubmissionRow.submitted_at, SubmissionRow.id)
            .limit(limit)
        )
        if not include_decided:
            reviewed = select(HumanReviewRow.grading_run_id)
            statement = statement.where(GradingRunRow.id.not_in(reviewed))

        return tuple(
            (
                Submission.model_validate(submission_row.document),
                GradingRun.model_validate(run_row.document),
            )
            for submission_row, run_row in self._session.execute(statement).all()
        )


class SqlJobQueue:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, job: GradingJob) -> GradingJob:
        existing = self.find_by_idempotency_key(job.idempotency_key)
        if existing is not None:
            return existing
        self._session.add(_job_row(job))
        try:
            self._session.flush()
        except IntegrityError:
            # 同時投入。一意制約が止めた側は既存を読んで返す。
            self._session.rollback()
            existing = self.find_by_idempotency_key(job.idempotency_key)
            if existing is None:  # pragma: no cover - 制約違反の原因が別
                raise
            return existing
        return job

    def reserve(
        self,
        now: datetime,
        *,
        worker: str,
        lease_seconds: float,
        subject_profile: str | None = None,
    ) -> GradingJob | None:
        """実行可能なジョブを 1 つ取る。

        **行ロックを取る**（`with_for_update(skip_locked=True)`）。取らないと、
        複数のワーカーが同じジョブを読んで両方が採点し、同じ提出に対する
        GradingRun が二重にできる。`skip_locked` なのは、他のワーカーが
        見ている行を待たずに次の仕事へ行くため。
        """
        queued = select(GradingJobRow).where(
            GradingJobRow.state == JobState.QUEUED.value,
            GradingJobRow.available_at <= now,
        )
        expired = select(GradingJobRow).where(
            GradingJobRow.state == JobState.RUNNING.value,
            GradingJobRow.lease_expires_at.is_not(None),
            GradingJobRow.lease_expires_at <= now,
        )
        if subject_profile is not None:
            queued = queued.where(GradingJobRow.subject_profile == subject_profile)
            expired = expired.where(GradingJobRow.subject_profile == subject_profile)

        for statement in (queued, expired):
            statement = statement.order_by(
                GradingJobRow.available_at, GradingJobRow.created_at, GradingJobRow.id
            ).limit(1)
            if self._session.bind is not None and self._session.bind.dialect.name != "sqlite":
                # SQLite は行ロックを持たない。単一プロセスの開発用なので許容する。
                statement = statement.with_for_update(skip_locked=True)
            row = self._session.execute(statement).scalars().first()
            if row is None:
                continue
            job = GradingJob.model_validate(row.document)
            reserved = job.reserved(now, worker=worker, lease_seconds=lease_seconds)
            _apply(row, reserved)
            self._session.flush()
            return reserved
        return None

    def update(self, job: GradingJob) -> None:
        row = self._session.get(GradingJobRow, str(job.id))
        if row is None:
            raise SubmissionStoreError(f"no job {job.id}")
        _apply(row, job)
        self._session.flush()

    def get(self, job_id: GradingJobId) -> GradingJob | None:
        row = self._session.get(GradingJobRow, str(job_id))
        return None if row is None else GradingJob.model_validate(row.document)

    def find_by_idempotency_key(self, key: str) -> GradingJob | None:
        row = (
            self._session.execute(select(GradingJobRow).where(GradingJobRow.idempotency_key == key))
            .scalars()
            .first()
        )
        return None if row is None else GradingJob.model_validate(row.document)

    def pending_count(self, subject_profile: str | None = None) -> int:
        statement = select(GradingJobRow).where(
            GradingJobRow.state.in_((JobState.QUEUED.value, JobState.RUNNING.value))
        )
        if subject_profile is not None:
            statement = statement.where(GradingJobRow.subject_profile == subject_profile)
        return len(self._session.execute(statement).scalars().all())


def _job_row(job: GradingJob) -> GradingJobRow:
    return GradingJobRow(
        id=str(job.id),
        tenant_id=str(job.tenant_id),
        submission_id=str(job.submission_id),
        subject_profile=job.subject_profile,
        reason=job.reason.value,
        idempotency_key=job.idempotency_key,
        state=job.state.value,
        attempts=job.attempts,
        available_at=job.available_at,
        lease_expires_at=job.lease_expires_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        document=_dump(job),
    )


def _apply(row: GradingJobRow, job: GradingJob) -> None:
    row.state = job.state.value
    row.attempts = job.attempts
    row.available_at = job.available_at
    row.lease_expires_at = job.lease_expires_at
    row.updated_at = job.updated_at
    row.document = _dump(job)


class SqlOutbox:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: DomainEvent) -> None:
        self._session.add(
            OutboxRow(
                event_id=str(event.event_id),
                tenant_id=str(event.tenant_id),
                type=event.type,
                occurred_at=event.occurred_at,
                published_at=None,
                document=_dump(event),
            )
        )
        self._session.flush()

    def unpublished(self, limit: int = 100) -> tuple[DomainEvent, ...]:
        rows = self._session.execute(
            select(OutboxRow)
            .where(OutboxRow.published_at.is_(None))
            .order_by(OutboxRow.occurred_at, OutboxRow.event_id)
            .limit(limit)
        ).scalars()
        return tuple(_revive(row) for row in rows)

    def mark_published(self, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        from datetime import UTC

        self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.event_id.in_([str(item) for item in event_ids]))
            .values(published_at=datetime.now(UTC))
        )
        self._session.flush()


def _revive(row: OutboxRow) -> DomainEvent:
    event_type = EVENT_TYPES.get(row.type)
    if event_type is None:
        raise SubmissionStoreError(f"unknown event type in the outbox: {row.type!r}")
    return event_type.model_validate(row.document)  # type: ignore[return-value]


class SqlTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_task(self, task: Task) -> None:
        row = self._session.get(TaskRow, str(task.id))
        if row is None:
            self._session.add(
                TaskRow(
                    id=str(task.id),
                    course_id=str(task.course_id),
                    document=_dump(task),
                )
            )
        else:
            row.course_id = str(task.course_id)
            row.document = _dump(task)
        self._session.flush()

    def get_task(self, task_id: TaskId) -> Task | None:
        row = self._session.get(TaskRow, str(task_id))
        return None if row is None else Task.model_validate(row.document)

    def save_version(self, version: TaskVersion) -> None:
        row = self._session.get(TaskVersionRow, str(version.id))
        if row is not None:
            if row.document == _dump(version):
                # 同じ内容の取り込みは冪等。決定的 ID の経路で普通に起きる。
                return
            raise TaskImmutabilityViolation(
                f"TaskVersion {version.id} already exists with different content; "
                "corrections create a new version (P8)"
            )
        self._session.add(
            TaskVersionRow(
                id=str(version.id),
                task_id=str(version.task_id),
                version=version.version,
                subject_profile=version.subject_profile,
                review_state=version.provenance.review_state.value,
                allow_handwriting=version.allow_handwriting,
                statement=version.statement,
                created_at=version.created_at,
                document=_dump(version),
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise TaskStoreError(
                f"task {version.task_id} already has a version {version.version}"
            ) from exc

    def get_version(self, version_id: TaskVersionId) -> TaskVersion | None:
        row = self._session.get(TaskVersionRow, str(version_id))
        return None if row is None else TaskVersion.model_validate(row.document)

    def latest_version(self, task_id: TaskId) -> TaskVersion | None:
        row = (
            self._session.execute(
                select(TaskVersionRow)
                .where(TaskVersionRow.task_id == str(task_id))
                .order_by(TaskVersionRow.version.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return None if row is None else TaskVersion.model_validate(row.document)

    def list_for_course(self, course_id: CourseId) -> tuple[Task, ...]:
        rows = self._session.execute(
            select(TaskRow).where(TaskRow.course_id == str(course_id)).order_by(TaskRow.id)
        ).scalars()
        return tuple(Task.model_validate(row.document) for row in rows)
