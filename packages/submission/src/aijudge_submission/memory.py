"""インメモリ実装。

テストと開発のためのもので、**本番では使わない**（プロセスが落ちれば消える）。
それでも規則は本番と同じにしてある。ここで緩めると、テストが通るのに
PostgreSQL 実装で落ちる、あるいはその逆が起きる。

特に守っているもの:

- `GradingRunRepository.save` は上書きを拒否する（P8）
- `supersede` は二度書き換えない
- `JobQueue.enqueue` は冪等キーで既存を返す
- `reserve` はリースの切れた RUNNING を取り直す
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aijudge_core import (
    BlindMark,
    Finalization,
    GradingRun,
    HumanReview,
    ReviewRequest,
    Submission,
)
from aijudge_core.events import DomainEvent
from aijudge_core.ids import (
    CourseId,
    GradingJobId,
    GradingRunId,
    HumanReviewId,
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)

from .jobs import GradingJob, GradingPhase, JobState
from .protocols import ImmutabilityViolation, RunDecision, SubmissionStoreError


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, payload: bytes) -> None:
        self._blobs[key] = payload

    def get(self, key: str) -> bytes:
        if key not in self._blobs:
            raise SubmissionStoreError(f"no artifact stored at {key!r}")
        return self._blobs[key]

    def exists(self, key: str) -> bool:
        return key in self._blobs


class InMemorySubmissionRepository:
    def __init__(self) -> None:
        self._items: dict[SubmissionId, Submission] = {}
        self._keys: dict[tuple[TenantId, str], SubmissionId] = {}
        self._tenants: dict[SubmissionId, TenantId] = {}

    def save(self, submission: Submission) -> None:
        existing = self._items.get(submission.id)
        if existing is not None and existing.submitted_at is not None:
            # 提出後は不変（core の規則）。書き換えようとするのは呼び出し側の誤り。
            raise ImmutabilityViolation(f"submission {submission.id} is already submitted")
        self._items[submission.id] = submission

    def get(self, submission_id: SubmissionId) -> Submission | None:
        return self._items.get(submission_id)

    def find_by_idempotency_key(self, tenant_id: TenantId, key: str) -> Submission | None:
        submission_id = self._keys.get((tenant_id, key))
        return None if submission_id is None else self._items.get(submission_id)

    def remember_idempotency_key(
        self, tenant_id: TenantId, key: str, submission_id: SubmissionId
    ) -> None:
        self._keys[(tenant_id, key)] = submission_id
        self._tenants[submission_id] = tenant_id

    def list_for_learner(
        self,
        tenant_id: TenantId,
        learner_id: UserId,
        task_version_id: TaskVersionId | None = None,
    ) -> tuple[Submission, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.learner_id == learner_id
                    and (task_version_id is None or item.task_version_id == task_version_id)
                    and self._tenants.get(item.id, tenant_id) == tenant_id
                ),
                key=lambda item: (item.created_at, item.id),
            )
        )

    def list_for_course(self, course_id: CourseId, *, limit: int = 5000) -> tuple[Submission, ...]:
        # インメモリ実装は課題を持たないので、コースでは絞れない。
        # 使うのは教員 UI（SQL 実装）だけなので、ここでは全件を返す。
        return tuple(self._items[key] for key in self._order)[:limit]

    def next_attempt(
        self, tenant_id: TenantId, learner_id: UserId, task_version_id: TaskVersionId
    ) -> int:
        return len(self.list_for_learner(tenant_id, learner_id, task_version_id)) + 1


class InMemoryGradingRunRepository:
    def __init__(self) -> None:
        self._items: dict[GradingRunId, GradingRun] = {}
        self._order: list[GradingRunId] = []

    def save(self, run: GradingRun) -> None:
        if run.id in self._items:
            raise ImmutabilityViolation(
                f"GradingRun {run.id} already exists; re-grading creates a new run (P8)"
            )
        self._items[run.id] = run
        self._order.append(run.id)

    def get(self, run_id: GradingRunId) -> GradingRun | None:
        return self._items.get(run_id)

    def list_for(self, submission_id: SubmissionId) -> tuple[GradingRun, ...]:
        return tuple(
            self._items[run_id]
            for run_id in self._order
            if self._items[run_id].submission_id == submission_id
        )

    def latest_for(self, submission_id: SubmissionId) -> GradingRun | None:
        runs = self.list_for(submission_id)
        return runs[-1] if runs else None

    def latest_for_many(
        self, submission_ids: Sequence[SubmissionId]
    ) -> dict[SubmissionId, GradingRun]:
        wanted = set(submission_ids)
        latest: dict[SubmissionId, GradingRun] = {}
        for run_id in self._order:
            run = self._items[run_id]
            if run.submission_id in wanted:
                # `_order` は保存順なので、後から来たものが最新。
                latest[run.submission_id] = run
        return latest

    def supersede(self, old_id: GradingRunId, new_id: GradingRunId) -> None:
        old = self._items.get(old_id)
        if old is None:
            raise SubmissionStoreError(f"no GradingRun {old_id}")
        if old.superseded_by is not None:
            raise ImmutabilityViolation(
                f"GradingRun {old_id} is already superseded by {old.superseded_by}"
            )
        self._items[old_id] = old.model_copy(update={"superseded_by": new_id})


class InMemoryReviewRepository:
    def __init__(self) -> None:
        self._reviews: dict[HumanReviewId, HumanReview] = {}
        self._by_run: dict[GradingRunId, HumanReviewId] = {}
        self._marks: dict[SubmissionId, BlindMark] = {}
        self._requests: dict[ReviewRequestId, ReviewRequest] = {}
        self._requests_by_run: dict[GradingRunId, ReviewRequestId] = {}
        self._finalizations: dict[GradingRunId, Finalization] = {}

    def save_review(self, review: HumanReview) -> None:
        self._reviews[review.id] = review
        self._by_run[review.grading_run_id] = review.id

    def get_review(self, review_id: HumanReviewId) -> HumanReview | None:
        return self._reviews.get(review_id)

    def find_review_for_run(self, run_id: GradingRunId) -> HumanReview | None:
        review_id = self._by_run.get(run_id)
        return None if review_id is None else self._reviews.get(review_id)

    def save_blind_mark(self, mark: BlindMark) -> None:
        if mark.submission_id in self._marks:
            raise ImmutabilityViolation(
                f"submission {mark.submission_id} already has a blind mark; "
                "overwriting it would let a post-AI grade become ground truth (ADR 0005)"
            )
        self._marks[mark.submission_id] = mark

    def find_blind_mark(self, submission_id: SubmissionId) -> BlindMark | None:
        return self._marks.get(submission_id)

    def save_request(self, request: ReviewRequest) -> None:
        if request.grading_run_id in self._requests_by_run:
            raise ImmutabilityViolation(
                f"GradingRun {request.grading_run_id} already has a review request"
            )
        self._requests[request.id] = request
        self._requests_by_run[request.grading_run_id] = request.id

    def find_request_for_run(self, run_id: GradingRunId) -> ReviewRequest | None:
        request_id = self._requests_by_run.get(run_id)
        return None if request_id is None else self._requests.get(request_id)

    def get_request(self, request_id: ReviewRequestId) -> ReviewRequest | None:
        return self._requests.get(request_id)

    def resolve_request(self, request_id: ReviewRequestId, review_id: HumanReviewId) -> None:
        request = self._requests.get(request_id)
        if request is not None:
            self._requests[request_id] = request.model_copy(update={"resolved_by": review_id})

    # -- 成績の確定 --

    def save_finalization(self, finalization: Finalization) -> None:
        if finalization.grading_run_id in self._finalizations:
            # 二度確定できると成績が二つ存在する。やり直しは再採点から。
            raise ImmutabilityViolation(
                f"GradingRun {finalization.grading_run_id} is already finalised"
            )
        self._finalizations[finalization.grading_run_id] = finalization

    def find_finalization_for_run(self, run_id: GradingRunId) -> Finalization | None:
        return self._finalizations.get(run_id)

    def decisions_for_runs(
        self, run_ids: Sequence[GradingRunId]
    ) -> dict[GradingRunId, RunDecision]:
        decisions: dict[GradingRunId, RunDecision] = {}
        for run_id in run_ids:
            review = self.find_review_for_run(run_id)
            request = self.find_request_for_run(run_id)
            finalization = self._finalizations.get(run_id)
            if review is None and request is None and finalization is None:
                continue
            decisions[run_id] = RunDecision(
                review=review, request=request, finalization=finalization
            )
        return decisions


class InMemoryJobQueue:
    def __init__(self) -> None:
        self._items: dict[GradingJobId, GradingJob] = {}
        self._keys: dict[str, GradingJobId] = {}

    def enqueue(self, job: GradingJob) -> GradingJob:
        existing_id = self._keys.get(job.idempotency_key)
        if existing_id is not None:
            # 同じ仕事は 1 つだけ。二重投入で GPU を二度回さない。
            return self._items[existing_id]
        self._items[job.id] = job
        self._keys[job.idempotency_key] = job.id
        return job

    def reserve(
        self,
        now: datetime,
        *,
        worker: str,
        lease_seconds: float,
        subject_profile: str | None = None,
        phase: GradingPhase | None = None,
    ) -> GradingJob | None:
        candidates = [
            job
            for job in self._items.values()
            if job.is_available(now)
            and (subject_profile is None or job.subject_profile == subject_profile)
            and (phase is None or job.phase is phase)
        ]
        if not candidates:
            return None
        # 古い順に取る。締切前でも先に出した学習者が先に返る。
        candidates.sort(key=lambda job: (job.available_at, job.created_at, job.id))
        reserved = candidates[0].reserved(now, worker=worker, lease_seconds=lease_seconds)
        self._items[reserved.id] = reserved
        return reserved

    def update(self, job: GradingJob) -> None:
        if job.id not in self._items:
            raise SubmissionStoreError(f"no job {job.id}")
        self._items[job.id] = job

    def get(self, job_id: GradingJobId) -> GradingJob | None:
        return self._items.get(job_id)

    def find_by_idempotency_key(self, key: str) -> GradingJob | None:
        job_id = self._keys.get(key)
        return None if job_id is None else self._items.get(job_id)

    def awaiting(self, submission_id: SubmissionId, phase: GradingPhase) -> bool:
        return any(
            job.submission_id == submission_id and job.phase is phase and not job.terminal
            for job in self._items.values()
        )

    def pending_count(
        self, subject_profile: str | None = None, phase: GradingPhase | None = None
    ) -> int:
        return sum(
            1
            for job in self._items.values()
            if job.state in (JobState.QUEUED, JobState.RUNNING)
            and (subject_profile is None or job.subject_profile == subject_profile)
            and (phase is None or job.phase is phase)
        )

    def release_waiting(self, submission_ids: Sequence[SubmissionId], now: datetime) -> int:
        """SQL 実装と同じ規則。**すでに取れるジョブは触らない。**"""
        targets = {str(i) for i in submission_ids}
        released = 0
        for job_id, job in list(self._items.items()):
            if (
                str(job.submission_id) in targets
                and job.state is JobState.QUEUED
                and job.available_at > now
            ):
                self._items[job_id] = job.model_copy(
                    update={"available_at": now, "updated_at": now}
                )
                released += 1
        return released

    def waiting_count(self, submission_ids: Sequence[SubmissionId], now: datetime) -> int:
        targets = {str(i) for i in submission_ids}
        return sum(
            1
            for job in self._items.values()
            if str(job.submission_id) in targets
            and job.state is JobState.QUEUED
            and job.available_at > now
        )

    def failed_for(self, submission_ids: Sequence[SubmissionId]) -> tuple[GradingJob, ...]:
        targets = {str(i) for i in submission_ids}
        return tuple(
            sorted(
                (
                    job
                    for job in self._items.values()
                    if str(job.submission_id) in targets and job.state is JobState.FAILED
                ),
                key=lambda job: job.updated_at,
                reverse=True,
            )
        )

    def all_jobs(self) -> tuple[GradingJob, ...]:
        return tuple(self._items.values())


class InMemoryOutbox:
    def __init__(self) -> None:
        self._events: list[DomainEvent] = []
        self._published: set[str] = set()

    def append(self, event: DomainEvent) -> None:
        self._events.append(event)

    def unpublished(self, limit: int = 100) -> tuple[DomainEvent, ...]:
        return tuple(event for event in self._events if event.event_id not in self._published)[
            :limit
        ]

    def mark_published(self, event_ids: Sequence[str]) -> None:
        self._published.update(event_ids)

    def all_events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._events)


class InMemoryUnitOfWork:
    """トランザクション境界の形だけを持つ実装。

    ロールバックは実装しない。**インメモリでロールバックできるふりをすると、
    DB 実装に移したときに初めて破綻する。** 使うのはテストと開発だけなので、
    境界の形（with と commit）を本番と同じにしておくことだけを目的にする。
    """

    def __init__(
        self,
        submissions: InMemorySubmissionRepository,
        runs: InMemoryGradingRunRepository,
        jobs: InMemoryJobQueue,
        outbox: InMemoryOutbox,
        reviews: InMemoryReviewRepository | None = None,
    ) -> None:
        self.submissions = submissions
        self.runs = runs
        self.jobs = jobs
        self.outbox = outbox
        self.reviews = reviews or InMemoryReviewRepository()
        self.commits = 0

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        raise NotImplementedError(
            "the in-memory unit of work cannot roll back; use the database implementation "
            "when you need real transactions"
        )


def in_memory_backend() -> tuple[InMemoryUnitOfWork, InMemoryArtifactStore]:
    """開発・テスト用の一式を組み立てる。"""
    uow = InMemoryUnitOfWork(
        InMemorySubmissionRepository(),
        InMemoryGradingRunRepository(),
        InMemoryJobQueue(),
        InMemoryOutbox(),
        InMemoryReviewRepository(),
    )
    return uow, InMemoryArtifactStore()
