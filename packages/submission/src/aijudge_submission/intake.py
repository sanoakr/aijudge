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
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Role,
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
from .protocols import (
    ArtifactStore,
    StreamingArtifactStore,
    UnitOfWork,
    artifact_storage_key,
)

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


class SubmissionTooLarge(SubmissionRejected):
    """上限を超えた。ストリーム受付では EOF を待たずにここで打ち切る。"""


def _capped(chunks: Iterable[bytes], max_bytes: int) -> Iterator[bytes]:
    """`max_bytes` を超えた時点で `SubmissionTooLarge` を投げる。

    全部読んでから長さを見ると、上限の意味が無い（数 GB を受け切ってしまう）。
    """
    seen = 0
    for chunk in chunks:
        seen += len(chunk)
        if seen > max_bytes:
            raise SubmissionTooLarge(f"ファイルが大きすぎます（上限 {max_bytes} バイト）")
        yield chunk


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
        stream_store: StreamingArtifactStore | None = None,
        clock: Clock = _utcnow,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        # 動画のような大きな添付をメモリに載せずに扱う置き場所。
        # 配備が用意していなければ `accept_stream` は断る。
        self._stream_store = stream_store
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
        grading_starts_at: datetime | None = None,
        submitted_as: Role = Role.LEARNER,
    ) -> AcceptResult:
        """提出を受け付ける。

        手書き画像（Phase 6）以外はここで一足飛びに SUBMITTED まで進む。
        書き起こしを挟む経路は `Submission.state` の遷移で表現され、
        確定するまで採点ジョブを積まない。

        `grading_starts_at` を渡すと、**ジョブは積むがその時刻まで走らせない**
        （試験・#67）。ここが受け取るのは時刻だけで、なぜ遅らせるのかは
        知らない ── 課題も締切もこの層には持ち込まない（`subject_profile` を
        文字列で受けているのと同じ）。

        `submitted_as` は**出した人のそのときの役割**（#108）。ここも同じで、
        役割を引くのは呼び出し側の仕事、記録に焼き付けるのがこの層の仕事。
        既定は学習者 ── 呼び忘れた経路が試行扱いになって静かに測定から
        消えるより、学習者として数えられて気づくほうがよい。
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
                    job = self._enqueue(
                        uow,
                        existing,
                        tenant_id,
                        subject_profile,
                        now,
                        starts_at=grading_starts_at,
                    )
                    uow.commit()
                return AcceptResult(submission=existing, job=job, deduplicated=True)

            submission_id = SubmissionId(new_id("sub"))
            attempt = uow.submissions.next_attempt(tenant_id, learner_id, task_version_id)
            artifacts = self._store_files(tenant_id, submission_id, files, now)

            submission = Submission(
                id=submission_id,
                task_version_id=task_version_id,
                learner_id=learner_id,
                submitted_as=submitted_as,
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
            job = self._enqueue(
                uow,
                submission,
                tenant_id,
                subject_profile,
                now,
                starts_at=grading_starts_at,
            )
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

    # -- ストリーム受付（動画）------------------------------------------------

    def accept_stream(
        self,
        *,
        tenant_id: TenantId,
        task_version_id: TaskVersionId,
        learner_id: UserId,
        subject_profile: str,
        filename: str,
        kind: ArtifactKind,
        chunks: Iterable[bytes],
        max_bytes: int,
        idempotency_key: str | None = None,
        grading_starts_at: datetime | None = None,
        submitted_as: Role = Role.LEARNER,
    ) -> AcceptResult:
        """動画のような大きな添付を **メモリに載せず** 受け付ける。

        `POST /submit` と違い、`chunks`（リクエスト本文の逐次列）をそのまま
        ストアへ流す。`max_bytes` を超えたら EOF を待たず打ち切る
        （`SubmissionTooLarge`）。

        冪等キー: クライアントが `Idempotency-Key` を送ればそれを使い、
        **本文を読む前に**二重投入を弾く（3 GB の再送を防ぐ）。無ければ
        ストリーム中に計算した SHA-256 から内容ベースのキーを作り、
        書いたあとで既存と突き合わせる（同一内容は新 attempt にしない）。

        1 提出 1 ファイルは既存経路と同じ。動画課題はコード課題とは別の
        課題（別ルーブリック）にすること ── 観点は `HUMAN_SCORED` で宣言し、
        教員が視聴して段階を入れる（総点は入るまで withheld・ADR 0015）。
        """
        if self._stream_store is None:
            raise SubmissionRejected("この配備は動画提出に対応していません")

        # 明示キーがあれば、本文を読む前に二重投入を弾く（3 GB の再送を防ぐ）。
        if idempotency_key is not None:
            hit = self.peek_idempotent(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                subject_profile=subject_profile,
                grading_starts_at=grading_starts_at,
            )
            if hit is not None:
                return hit

        submission_id = SubmissionId(new_id("sub"))
        artifact_id = ArtifactId(new_id("art"))
        key = artifact_storage_key(tenant_id, submission_id, artifact_id, filename)

        blob = self._stream_store.put_stream(key, _capped(chunks, max_bytes))
        try:
            return self.record_streamed(
                tenant_id=tenant_id,
                task_version_id=task_version_id,
                learner_id=learner_id,
                subject_profile=subject_profile,
                filename=filename,
                kind=kind,
                submission_id=submission_id,
                artifact_id=artifact_id,
                storage_key=key,
                byte_size=blob.byte_size,
                sha256=blob.sha256,
                idempotency_key=idempotency_key,
                grading_starts_at=grading_starts_at,
                submitted_as=submitted_as,
            )
        except BaseException:
            self._stream_store.delete(key)
            raise

    def record_streamed(
        self,
        *,
        tenant_id: TenantId,
        task_version_id: TaskVersionId,
        learner_id: UserId,
        subject_profile: str,
        filename: str,
        kind: ArtifactKind,
        submission_id: SubmissionId,
        artifact_id: ArtifactId,
        storage_key: str,
        byte_size: int,
        sha256: str,
        idempotency_key: str | None = None,
        grading_starts_at: datetime | None = None,
        submitted_as: Role = Role.LEARNER,
    ) -> AcceptResult:
        """既にストアへ書かれた blob から提出・ジョブ・イベントを作る。

        ストリーム書き込みは Web ルート側でイベントループを塞がずに行い、
        DB の一貫した書き込みだけをここに集める。二重投入時は
        `deduplicated=True` を返す ── **書いた blob の削除は呼び出し側**
        （どの経路も孤児を残さないため、`accept_stream` と route の両方で消す）。
        """
        if self._stream_store is None:
            raise SubmissionRejected("この配備は動画提出に対応していません")
        if byte_size == 0:
            raise SubmissionRejected("ファイルが空です")
        now = self._clock()
        content_key = idempotency_key or f"sha256:{sha256}:{task_version_id}:{learner_id}"

        with self._uow_factory() as uow:
            hit = self._existing_for_key(
                uow, tenant_id, content_key, subject_profile, now, grading_starts_at
            )
            if hit is not None:
                self._stream_store.delete(storage_key)
                return hit

            attempt = uow.submissions.next_attempt(tenant_id, learner_id, task_version_id)
            artifact = Artifact(
                id=artifact_id,
                submission_id=submission_id,
                role=ArtifactRole.ORIGINAL,
                kind=kind,
                filename=filename,
                storage_key=storage_key,
                content_hash=f"sha256:{sha256}",
                byte_size=byte_size,
                created_at=now,
            )
            submission = Submission(
                id=submission_id,
                task_version_id=task_version_id,
                learner_id=learner_id,
                submitted_as=submitted_as,
                state=SubmissionState.DRAFT,
                attempt=attempt,
                artifacts=(artifact,),
                created_at=now,
            )
            assert_transition(submission.state, SubmissionState.SUBMITTED)
            submission = submission.model_copy(
                update={"state": SubmissionState.SUBMITTED, "submitted_at": now}
            )
            submission = Submission.model_validate(submission.model_dump())

            uow.submissions.save(submission)
            uow.submissions.remember_idempotency_key(tenant_id, content_key, submission.id)
            job = self._enqueue(
                uow, submission, tenant_id, subject_profile, now, starts_at=grading_starts_at
            )
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

    def peek_idempotent(
        self,
        *,
        tenant_id: TenantId,
        idempotency_key: str,
        subject_profile: str,
        grading_starts_at: datetime | None = None,
    ) -> AcceptResult | None:
        """冪等キーで既存の提出を引く（本文を読む前の事前チェック用）。

        動画ルートが `Idempotency-Key` で再送を **3 GB 読む前に** 弾くのに使う。
        """
        with self._uow_factory() as uow:
            return self._existing_for_key(
                uow, tenant_id, idempotency_key, subject_profile, self._clock(), grading_starts_at
            )

    def _existing_for_key(
        self,
        uow: UnitOfWork,
        tenant_id: TenantId,
        key: str,
        subject_profile: str,
        now: datetime,
        grading_starts_at: datetime | None,
    ) -> AcceptResult | None:
        """冪等キーで既存の提出を引き、あればジョブを補って返す。"""
        existing = uow.submissions.find_by_idempotency_key(tenant_id, key)
        if existing is None:
            return None
        job = uow.jobs.find_by_idempotency_key(
            job_idempotency_key(existing.id, JobReason.SUBMISSION)
        )
        if job is None:
            job = self._enqueue(
                uow, existing, tenant_id, subject_profile, now, starts_at=grading_starts_at
            )
            uow.commit()
        return AcceptResult(submission=existing, job=job, deduplicated=True)

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
        starts_at: datetime | None = None,
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
                # **過去の時刻で遅らせない。** 試験が終わったあとに出された
                # 提出は、待たせる理由が無いのでそのまま採点する。
                available_at=max(now, starts_at) if starts_at else now,
                created_at=now,
                updated_at=now,
            )
        )
