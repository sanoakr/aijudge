"""S3 が要求する外部との境界。

保存先とキューの実装をここから追い出すのが目的。Phase 0 は
インメモリとファイルで動かし、PostgreSQL / MinIO へは実装を差し替えるだけで
移れるようにする。S4（サンドボックス）で同じやり方を採ったのと同じ理由で、
「動く実装」と「本番の実装」を型で揃えておく。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core import (
    Artifact,
    BlindMark,
    Finalization,
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
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)

from .jobs import GradingJob, GradingPhase


class SubmissionStoreError(Exception):
    """保存先の不整合。呼び出し側の誤りと区別するため専用の型にする。"""


class ImmutabilityViolation(SubmissionStoreError):
    """不変であるべき記録を書き換えようとした（設計原則 P8）。"""


@dataclass(frozen=True)
class RunDecision:
    """1 つの採点に付いた人間側の記録。一覧画面のための読み取り専用の束。

    3 つは別々の表に載っているが、**1 つの採点について 3 つとも見ないと
    段階が決まらない**（ADR 0010）。確定は `finalization`、教員が読んだ事実は
    `review`、学習者の異議は `request` で、どれか 1 つだけを引くと「誰も
    読んでいない自動確定」が「教員が確認した成績」に見える。まとめて返す。
    """

    review: HumanReview | None = None
    request: ReviewRequest | None = None
    finalization: Finalization | None = None


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

    def list_for_course(self, course_id: CourseId, *, limit: int = 5000) -> tuple[Submission, ...]:
        """このコースの全提出。**教員の一覧のためにある。**

        提出は課題版を指しており、コースを直接持たない（持たせると課題の
        移動で片方だけ古くなる）ので、課題 → コースの経路で絞る。
        """
        ...

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

    def latest_for_many(
        self, submission_ids: Sequence[SubmissionId]
    ) -> dict[SubmissionId, GradingRun]:
        """複数の提出の最新採点をまとめて引く。一覧画面のためにある。

        提出 1 件ずつ `latest_for` を呼ぶと、課題数 × 提出回数のクエリに
        なる。**採点が無い提出は結果に現れない**（None を詰めない）ので、
        呼び出し側は `.get()` で受ける。
        """
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
    """教員の確認・修正、blind 採点、そして成績の確定。

    **`Finalization` の存在が成績の確定を意味する**（`HumanReview` ではない）。
    確定は教員の個別レビューからも、課題単位の一括操作からも、締切経過による
    自動確定からも起きる。`HumanReview` はそのうち「教員が 1 件を読んだ」
    場合にだけ生まれる記録で、一致度の測定が使えるのはこちらだけ
    （ADR 0010）。確定が無いうちは AI の判定は暫定で、学習者に示す範囲は
    上位層が決める（設計原則 P5）。

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

    # -- 成績の確定 --

    def save_finalization(self, finalization: Finalization) -> None:
        """確定を保存する。同じ採点に二度目は拒否する。

        二度確定できると成績が二つ存在する。やり直しは再採点から。
        """
        ...

    def find_finalization_for_run(self, run_id: GradingRunId) -> Finalization | None: ...

    def decisions_for_runs(
        self, run_ids: Sequence[GradingRunId]
    ) -> dict[GradingRunId, RunDecision]:
        """複数の採点について、レビュー・依頼・確定をまとめて引く。

        一覧画面のためにある。1 件ずつ 3 回引くと採点数 × 3 のクエリになる。
        何も付いていない採点は結果に現れない。
        """
        ...

    def unfinalized_for_task(
        self, task_id: TaskId, *, limit: int = 500
    ) -> tuple[tuple[Submission, GradingRun, ReviewRequest | None], ...]:
        """この課題でまだ確定していない提出。一括確定と自動確定が読む。

        提出のたびに 1 行ではなく、**最新の採点 1 件につき 1 行**。
        未対応の異議申立を判定できるよう、依頼も一緒に返す。
        """
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
        phase: GradingPhase | None = None,
    ) -> GradingJob | None:
        """実行可能なジョブを 1 つ取る。無ければ None。

        リースの切れた RUNNING も取り直しの対象にする。
        """
        ...

    def update(self, job: GradingJob) -> None: ...

    def get(self, job_id: str) -> GradingJob | None: ...

    def find_by_idempotency_key(self, key: str) -> GradingJob | None: ...

    def awaiting(self, submission_id: SubmissionId, phase: GradingPhase) -> bool:
        """この提出にその段階の未了ジョブがあるか。

        「AI 評価がまだ来ていない」と「AI 評価が失敗して観点が空のまま」を
        画面で区別するために要る。前者は待てばよく、後者は教員が埋める。
        """
        ...

    def pending_count(
        self, subject_profile: str | None = None, phase: GradingPhase | None = None
    ) -> int:
        """待ち行列の長さ。締切前の滞留を見るのに使う。"""
        ...

    def release_waiting(self, submission_ids: Sequence[SubmissionId], now: datetime) -> int:
        """採点開始時刻まで寝かせてあるジョブを、いま取れる状態にする。

        試験の一括採点（#67）。**すでに取れるジョブは触らない** ── 触ると
        走っている最中のリースを巻き戻す。変えた件数を返す。
        """
        ...

    def waiting_count(self, submission_ids: Sequence[SubmissionId], now: datetime) -> int:
        """まだ寝かせてあるジョブの件数。押す前に「何件動くか」を出すため。"""
        ...

    def failed_for(self, submission_ids: Sequence[SubmissionId]) -> tuple[GradingJob, ...]:
        """リトライ上限まで落ちたジョブ。

        一括採点で一部が落ちたとき、**気づけないと成績が欠けたまま確定する**。
        """
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
