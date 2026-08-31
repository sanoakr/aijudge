"""提出の受付（S3）。

ここが「学習者が出した」を「採点すべき仕事がある」に変える唯一の場所。

守ること:

- **二重投入で提出が増えない。** ブラウザの二度押し、POST のリトライ、
  リレーの再送はすべて日常的に起きる。防ぐのではなく、起きても害が無い形にする。
- **提出とジョブとイベントが同時に成立する。** 片方だけ成立すると、
  採点されない提出か、存在しない提出の採点が生まれる。
- **採点はレビューを待たない。** 提出時にジョブが入り、結果はあとから届く
  （設計方針 §10 / ADR 0007）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Submission,
    SubmissionCreated,
    SubmissionState,
    assert_transition,
    new_id,
)
from aijudge_core.ids import (
    ArtifactId,
    EventId,
    GradingJobId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)

from .jobs import (
    DEFAULT_MAX_ATTEMPTS,
    GradingJob,
    JobReason,
    job_idempotency_key,
)
from .protocols import ArtifactStore, UnitOfWork, artifact_storage_key

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IncomingFile(BaseModel):
    """受け付ける 1 ファイル。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = Field(min_length=1)
    kind: ArtifactKind
    payload: bytes
    role: ArtifactRole = ArtifactRole.ORIGINAL


class SubmissionRejected(Exception):
    """受け付けられない提出。学習者に理由を返せる形にする。"""


class AcceptResult(BaseModel):
    """受付の結果。"""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    submission: Submission
    job: GradingJob
    # 既存の提出をそのまま返したか（二重投入だったか）。
    # UI が「提出しました」と「すでに提出済みです」を出し分けるのに使う。
    deduplicated: bool = False


def content_idempotency_key(
    task_version_id: TaskVersionId, learner_id: UserId, files: Sequence[IncomingFile]
) -> str:
    """提出内容から決まる冪等キー。

    同じ課題・同じ学習者・同じ中身なら同じキー。学習者が同じコードを
    二度出したのは新しい挑戦ではないので、提出を増やさない。

    中身を変えて出し直したら別のキーになり、新しい提出（attempt+1）になる。
    """
    digest = hashlib.sha256()
    digest.update(str(task_version_id).encode())
    digest.update(b"\x1e")
    digest.update(str(learner_id).encode())
    for item in sorted(files, key=lambda f: (f.role.value, f.filename)):
        digest.update(b"\x1e")
        digest.update(item.role.value.encode())
        digest.update(b"\x00")
        digest.update(item.filename.encode())
        digest.update(b"\x00")
        digest.update(hashlib.sha256(item.payload).hexdigest().encode())
    return f"sha256:{digest.hexdigest()}"


class SubmissionService:
    """提出を受け付け、採点ジョブを投入する。

    採点エンジンを知らない。ジョブを積むところまでが仕事で、
    それを消費するのは app 層のワーカー（ADR 0001 / 設計方針 §2.3）。
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        store: ArtifactStore,
        *,
        clock: Clock = _utcnow,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._clock = clock
        self._max_attempts = max_attempts

    # -- 受付 --------------------------------------------------------------

    def accept(
        self,
        *,
        tenant_id: TenantId,
        task_version_id: TaskVersionId,
        learner_id: UserId,
        subject_profile: str,
        files: Sequence[IncomingFile],
        idempotency_key: str | None = None,
    ) -> AcceptResult:
        """提出を受け付ける。

        手書き画像（Phase 6）以外はここで一足飛びに SUBMITTED まで進む。
        書き起こしを挟む経路は `Submission.state` の遷移で表現され、
        確定するまで採点ジョブを積まない。
        """
        if not files:
            raise SubmissionRejected("提出物がありません")

        key = idempotency_key or content_idempotency_key(task_version_id, learner_id, files)
        now = self._clock()

        with self._uow_factory() as uow:
            existing = uow.submissions.find_by_idempotency_key(tenant_id, key)
            if existing is not None:
                # 二重投入。既存の提出とそのジョブを返す。**新しく作らない。**
                job = uow.jobs.find_by_idempotency_key(
                    job_idempotency_key(existing.id, JobReason.SUBMISSION)
                )
                if job is None:
                    # 提出は残ったがジョブが入らなかった（前回の投入が
                    # 途中で落ちた）。ここで補う。取りこぼした提出を
                    # 永久に未採点で放置しないため。
                    job = self._enqueue(uow, existing, tenant_id, subject_profile, now)
                    uow.commit()
                return AcceptResult(submission=existing, job=job, deduplicated=True)

            submission_id = SubmissionId(new_id("sub"))
            attempt = uow.submissions.next_attempt(tenant_id, learner_id, task_version_id)
            artifacts = self._store_files(tenant_id, submission_id, files, now)

            submission = Submission(
                id=submission_id,
                task_version_id=task_version_id,
                learner_id=learner_id,
                state=SubmissionState.DRAFT,
                attempt=attempt,
                artifacts=artifacts,
                created_at=now,
            )
            assert_transition(submission.state, SubmissionState.SUBMITTED)
            submission = submission.model_copy(
                update={"state": SubmissionState.SUBMITTED, "submitted_at": now}
            )
            # 検証を通す（提出後の不変条件はここで初めて効く）。
            submission = Submission.model_validate(submission.model_dump())

            uow.submissions.save(submission)
            uow.submissions.remember_idempotency_key(tenant_id, key, submission.id)
            job = self._enqueue(uow, submission, tenant_id, subject_profile, now)
            uow.outbox.append(
                SubmissionCreated(
                    event_id=EventId(new_id("evt")),
                    tenant_id=tenant_id,
                    occurred_at=now,
                    submission_id=submission.id,
                    task_version_id=task_version_id,
                    learner_id=learner_id,
                    attempt=submission.attempt,
                    subject_profile=subject_profile,
                )
            )
            uow.commit()

        return AcceptResult(submission=submission, job=job, deduplicated=False)

    # -- 再採点 ------------------------------------------------------------

    def request_regrade(
        self,
        *,
        tenant_id: TenantId,
        submission_id: SubmissionId,
        subject_profile: str,
        discriminator: str = "",
        task_version_id: TaskVersionId | None = None,
    ) -> GradingJob:
        """再採点を積む。

        `discriminator` にモデル版やプロンプト版を入れる。同じ版で二度
        流し直してもジョブは増えない（モデル更新時の全件再採点を、
        途中で落ちても安全に再開できるようにするため）。

        `task_version_id` を渡すと**その課題版で**採点し直す。渡さなければ
        提出が指している版（＝出したときの版）で採点し直す。実施中に課題を
        訂正したとき、既に出ている提出を新しい版で採点し直すのがこの経路で、
        **教員が明示的に押したときだけ動く**（誰も押していない再採点で成績が
        動かない・設計原則 P5）。

        版を指定したときは、**その版を冪等キーに混ぜる**。混ぜないと、
        以前の再採点と同じキーになってジョブが積まれない ── 訂正のたびに
        押しても 2 度目から何も起きない、という壊れ方をする。

        過去の採点は消さない。新しい採点が終わった時点で、旧採点に
        `superseded_by` が入る（P8）。
        """
        now = self._clock()
        with self._uow_factory() as uow:
            submission = uow.submissions.get(submission_id)
            if submission is None:
                raise SubmissionRejected(f"提出が見つかりません: {submission_id}")
            if submission.state is not SubmissionState.SUBMITTED:
                raise SubmissionRejected("確定していない提出は採点できません")

            job = self._enqueue(
                uow,
                submission,
                tenant_id,
                subject_profile,
                now,
                task_version_id=task_version_id,
                reason=JobReason.REGRADE,
                discriminator=discriminator,
            )
            uow.commit()
        return job

    # -- internals ---------------------------------------------------------

    def _store_files(
        self,
        tenant_id: TenantId,
        submission_id: SubmissionId,
        files: Sequence[IncomingFile],
        now: datetime,
    ) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for item in files:
            artifact_id = ArtifactId(new_id("art"))
            key = artifact_storage_key(tenant_id, submission_id, artifact_id, item.filename)
            # 中身を先に置く。メタデータだけ残って中身が無い状態を作らない
            # （その提出は永久に採点できず、原因も追えない）。
            self._store.put(key, item.payload)
            artifacts.append(
                Artifact(
                    id=artifact_id,
                    submission_id=submission_id,
                    role=item.role,
                    kind=item.kind,
                    filename=item.filename,
                    storage_key=key,
                    content_hash=f"sha256:{hashlib.sha256(item.payload).hexdigest()}",
                    byte_size=len(item.payload),
                    created_at=now,
                )
            )
        return tuple(artifacts)

    def _enqueue(
        self,
        uow: UnitOfWork,
        submission: Submission,
        tenant_id: TenantId,
        subject_profile: str,
        now: datetime,
        *,
        reason: JobReason = JobReason.SUBMISSION,
        discriminator: str = "",
        task_version_id: TaskVersionId | None = None,
    ) -> GradingJob:
        target = task_version_id or submission.task_version_id
        # 版を指定したときは冪等キーに混ぜる（同上の理由）。
        mixed = discriminator if task_version_id is None else f"{discriminator}:{target}"
        key = job_idempotency_key(submission.id, reason, discriminator=mixed)
        return uow.jobs.enqueue(
            GradingJob(
                id=GradingJobId(new_id("job")),
                tenant_id=tenant_id,
                submission_id=submission.id,
                task_version_id=target,
                subject_profile=subject_profile,
                reason=reason,
                idempotency_key=key,
                max_attempts=self._max_attempts,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
