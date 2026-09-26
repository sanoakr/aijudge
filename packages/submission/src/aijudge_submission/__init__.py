"""aiJudge submission (S3) — 提出受付と採点ジョブのオーケストレーション。

**採点エンジン（S5）を import しない。** S3 → S5 の結合はイベント
（`SubmissionCreated`）だけで、両者を束ねるのは app 層（ADR 0001）。
ここが直接パイプラインを呼ぶと、提出受付が採点の実装に引きずられ、
採点を止めると提出も受け付けられなくなる。

保存先はプロトコルにしてある。Phase 0 はインメモリとファイルで動かし、
PostgreSQL / MinIO へは実装を差し替えるだけで移る（S4 と同じ方式）。
"""

from __future__ import annotations

from aijudge_core import GradingPhase

from .filestore import FilesystemArtifactStore, StoredBlob, iter_file, parse_range
from .intake import (
    AcceptResult,
    IncomingFile,
    SubmissionRejected,
    SubmissionService,
    SubmissionTooLarge,
    content_idempotency_key,
)
from .jobs import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    LEASE_LOST_ERROR,
    GradingJob,
    JobReason,
    JobState,
    follow_up_idempotency_key,
    job_idempotency_key,
)
from .memory import (
    InMemoryArtifactStore,
    InMemoryGradingRunRepository,
    InMemoryJobQueue,
    InMemoryOutbox,
    InMemoryReviewRepository,
    InMemorySubmissionRepository,
    InMemoryUnitOfWork,
    in_memory_backend,
)
from .protocols import (
    ArtifactStore,
    AttentionCounts,
    CourseReviewQueries,
    GradingRunRepository,
    ImmutabilityViolation,
    JobQueue,
    Outbox,
    ReviewRepository,
    ReviewStore,
    RunDecision,
    StreamingArtifactStore,
    SubmissionCounts,
    SubmissionRepository,
    SubmissionStoreError,
    UnitOfWork,
    artifact_storage_key,
    gradable_contents,
)
from .resumable import (
    DEFAULT_TTL,
    FilesystemUploadSessions,
    OffsetMismatch,
    TooLarge,
    UploadSession,
    UploadSessionError,
)

__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TTL",
    "LEASE_LOST_ERROR",
    "AcceptResult",
    "ArtifactStore",
    "AttentionCounts",
    "CourseReviewQueries",
    "FilesystemArtifactStore",
    "FilesystemUploadSessions",
    "GradingJob",
    "GradingPhase",
    "GradingRunRepository",
    "ImmutabilityViolation",
    "InMemoryArtifactStore",
    "InMemoryGradingRunRepository",
    "InMemoryJobQueue",
    "InMemoryOutbox",
    "InMemoryReviewRepository",
    "InMemorySubmissionRepository",
    "InMemoryUnitOfWork",
    "IncomingFile",
    "JobQueue",
    "JobReason",
    "JobState",
    "OffsetMismatch",
    "Outbox",
    "ReviewRepository",
    "ReviewStore",
    "RunDecision",
    "StoredBlob",
    "StreamingArtifactStore",
    "SubmissionCounts",
    "SubmissionRejected",
    "SubmissionRepository",
    "SubmissionService",
    "SubmissionStoreError",
    "SubmissionTooLarge",
    "TooLarge",
    "UnitOfWork",
    "UploadSession",
    "UploadSessionError",
    "artifact_storage_key",
    "content_idempotency_key",
    "follow_up_idempotency_key",
    "gradable_contents",
    "in_memory_backend",
    "iter_file",
    "job_idempotency_key",
    "parse_range",
]
