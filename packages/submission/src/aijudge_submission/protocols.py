"""S3 が要求する外部との境界。

保存先とキューの実装をここから追い出すのが目的。Phase 0 は
インメモリとファイルで動かし、PostgreSQL / MinIO へは実装を差し替えるだけで
移れるようにする。S4（サンドボックス）で同じやり方を採ったのと同じ理由で、
「動く実装」と「本番の実装」を型で揃えておく。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core import (
    Artifact,
    BlindMark,
    GradingRun,
    HumanReview,
    ReviewRequest,
    Submission,
)
from aijudge_core.events import DomainEvent
from aijudge_core.ids import (
    ArtifactId,
    CourseId,
    GradingRunId,
    HumanReviewId,
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)

from .jobs import GradingJob


class SubmissionStoreError(Exception):
    """保存先の不整合。呼び出し側の誤りと区別するため専用の型にする。"""


class ImmutabilityViolation(SubmissionStoreError):
    """不変であるべき記録を書き換えようとした（設計原則 P8）。"""


@runtime_checkable
class ArtifactStore(Protocol):
    """提出物の中身の置き場所。

    メタデータ（`Artifact`）と中身を分けるのは、中身がオブジェクト
    ストレージに、メタデータが RDB に載るため。`storage_key` が両者を繋ぐ。
    """

    def put(self, key: str, payload: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


@runtime_checkable
class SubmissionRepository(Protocol):
    """提出のメタデータ。"""

    def save(self, submission: Submission) -> None: ...

    def get(self, submission_id: SubmissionId) -> Submission | None: ...

    def find_by_idempotency_key(self, tenant_id: TenantId, key: str) -> Submission | None:
        """冪等キーで既存の提出を引く。

        二重投入は事故ではなく日常（POST のリトライ、ブラウザの二度押し）。
        新しい提出を作る前に必ずここを見る。
        """
        ...

    def list_for_learner(
        self, tenant_id: TenantId, learner_id: UserId, task_version_id: TaskVersionId | None = None
    ) -> tuple[Submission, ...]: ...

    def next_attempt(
        self, tenant_id: TenantId, learner_id: UserId, task_version_id: TaskVersionId
    ) -> int:
        """この学習者のこの課題に対する次の提出回数。"""
        ...

    def remember_idempotency_key(
        self, tenant_id: TenantId, key: str, submission_id: SubmissionId
    ) -> None:
        """冪等キーと提出の対応を記録する。

        提出の保存と同じトランザクションで行う。ここが別トランザクション
        だと、保存できたのにキーが残らず、次の再送で提出が二重に増える。
        """
        ...


@runtime_checkable
class GradingRunRepository(Protocol):
    """採点結果。**追記のみ。**"""

    def save(self, run: GradingRun) -> None:
        """新しい採点を保存する。既存の run と同じ ID なら拒否する（P8）。"""
        ...

    def get(self, run_id: GradingRunId) -> GradingRun | None: ...

    def latest_for(self, submission_id: SubmissionId) -> GradingRun | None:
        """最新の採点。再採点していれば新しい方。"""
        ...

    def list_for(self, submission_id: SubmissionId) -> tuple[GradingRun, ...]:
        """この提出の全採点。古い順。過去の採点も消さない（P8）。"""
        ...

    def supersede(self, old_id: GradingRunId, new_id: GradingRunId) -> None:
        """旧採点に「これに置き換わった」と記す。

        **保存済みの GradingRun に許される唯一の変更。** `superseded_by` は
        新しい採点が生まれて初めて確定する値で、旧採点の側に書くしかない。
        既に設定済みなら拒否する（二度書き換えられるなら不変ではない）。
        """
        ...


@runtime_checkable
class ReviewRepository(Protocol):
    """教員の確認・修正と、blind 採点。

    `HumanReview` の存在が成績の確定を意味する。無いうちは AI の判定は
    暫定で、学習者に示す範囲は上位層が決める（設計原則 P5）。

    `BlindMark` は測定用の正解データ（ADR 0005）。抽出された提出にしか
    存在しないので、無いのは正常な状態である。
    """

    def save_review(self, review: HumanReview) -> None: ...

    def get_review(self, review_id: HumanReviewId) -> HumanReview | None: ...

    def find_review_for_run(self, run_id: GradingRunId) -> HumanReview | None: ...

    def save_blind_mark(self, mark: BlindMark) -> None:
        """blind 採点を保存する。既にあれば拒否する。

        二度目を受け付けると、AI を見たあとの段階で上書きできてしまい、
        正解データが汚れる（ADR 0005）。
        """
        ...

    def find_blind_mark(self, submission_id: SubmissionId) -> BlindMark | None: ...

    # -- 学習者からの再確認の依頼 --

    def save_request(self, request: ReviewRequest) -> None:
        """レビュー依頼を保存する。同じ採点に二重に出させない。"""
        ...

    def find_request_for_run(self, run_id: GradingRunId) -> ReviewRequest | None: ...

    def get_request(self, request_id: ReviewRequestId) -> ReviewRequest | None: ...

    def resolve_request(self, request_id: ReviewRequestId, review_id: HumanReviewId) -> None:
        """依頼に対応した教員レビューを結びつける。"""
        ...

    def requested_for_course(
        self, course_id: CourseId, *, include_resolved: bool = False, limit: int = 200
    ) -> tuple[tuple[Submission, GradingRun, ReviewRequest], ...]:
        """学習者が再確認を依頼した提出。教員の待ち行列。"""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """採点ジョブのキュー。

    Phase 0 はインメモリ／DB のポーリング。Redis Streams に載せ替えても
    この形は変わらない。
    """

    def enqueue(self, job: GradingJob) -> GradingJob:
        """投入する。同じ冪等キーのジョブがあれば**それを返す**（新規作成しない）。"""
        ...

    def reserve(
        self,
        now: datetime,
        *,
        worker: str,
        lease_seconds: float,
        subject_profile: str | None = None,
    ) -> GradingJob | None:
        """実行可能なジョブを 1 つ取る。無ければ None。

        リースの切れた RUNNING も取り直しの対象にする。
        """
        ...

    def update(self, job: GradingJob) -> None: ...

    def get(self, job_id: str) -> GradingJob | None: ...

    def find_by_idempotency_key(self, key: str) -> GradingJob | None: ...

    def pending_count(self, subject_profile: str | None = None) -> int:
        """待ち行列の長さ。締切前の滞留を見るのに使う。"""
        ...


@runtime_checkable
class Outbox(Protocol):
    """ドメインイベントの送信箱（Outbox パターン）。

    提出の保存とイベントの記録を同じトランザクションに入れるためにある。
    保存できたのにイベントが出ない、あるいは逆が起きると、採点されない提出か
    存在しない提出の採点が生まれる。
    """

    def append(self, event: DomainEvent) -> None: ...

    def unpublished(self, limit: int = 100) -> tuple[DomainEvent, ...]: ...

    def mark_published(self, event_ids: Sequence[str]) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """1 つのトランザクション境界。

    提出・ジョブ・イベントは同時に成立しなければならない。インメモリ実装では
    何もしないが、境界を型で持っておかないと DB 実装で後から入れられない。
    """

    submissions: SubmissionRepository
    runs: GradingRunRepository
    reviews: ReviewRepository
    jobs: JobQueue
    outbox: Outbox

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, *exc: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def artifact_storage_key(
    tenant_id: TenantId, submission_id: SubmissionId, artifact_id: ArtifactId, filename: str | None
) -> str:
    """オブジェクトストレージ上のキー。

    テナントを先頭に置くのは、後からテナント単位で移動・削除・容量計算を
    するときにプレフィックス走査で済むため（Phase 8 で効く）。
    """
    suffix = f"/{filename}" if filename else ""
    return f"{tenant_id}/{submission_id}/{artifact_id}{suffix}"


def gradable_contents(submission: Submission, store: ArtifactStore) -> dict[ArtifactId, bytes]:
    """採点対象 Artifact の中身を読む。

    採点パイプラインの `ContentLoader` に渡す形に揃える。
    書き起こしは学習者が確定させたものだけが対象になる（core 側の規則）。
    """
    return {
        artifact.id: store.get(artifact.storage_key) for artifact in submission.gradable_artifacts
    }


def artifact_for(
    submission: Submission, artifact_id: ArtifactId
) -> Artifact | None:  # pragma: no cover - 補助
    return next((a for a in submission.artifacts if a.id == artifact_id), None)
