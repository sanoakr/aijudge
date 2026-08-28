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

from .filestore import FilesystemArtifactStore
from .intake import (
    AcceptResult,
    IncomingFile,
    SubmissionRejected,
    SubmissionService,
    content_idempotency_key,
)
from .jobs import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    GradingJob,
    JobReason,
    JobState,
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
    GradingRunRepository,
    ImmutabilityViolation,
    JobQueue,
    Outbox,
    ReviewRepository,
    SubmissionRepository,
    SubmissionStoreError,
    UnitOfWork,
    artifact_storage_key,
    gradable_contents,
)

__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "AcceptResult",
    "ArtifactStore",
    "FilesystemArtifactStore",
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
    "Outbox",
    "ReviewRepository",
    "SubmissionRejected",
    "SubmissionRepository",
    "SubmissionService",
    "SubmissionStoreError",
    "UnitOfWork",
    "artifact_storage_key",
    "content_idempotency_key",
    "gradable_contents",
    "in_memory_backend",
    "job_idempotency_key",
]
