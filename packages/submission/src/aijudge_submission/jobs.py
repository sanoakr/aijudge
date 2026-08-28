"""採点ジョブ（S3）。

採点は提出のたびに走るが、**同期では返せない**。AI 評価は 1 観点あたり
数十秒かかり、締切前はバーストする。設計方針 §10 の「AI 評価は非同期で
あとから届く」を成立させるのがこのモジュールの役割。

守る性質は 3 つ。

冪等   同じ提出を二重に採点しない。二重投入は事故ではなく日常（POST の
       リトライ、リレーの再送、ワーカーの再起動）なので、防ぐのではなく
       起きても害が無い形にする。
リトライ 一時的な失敗（LLM のタイムアウト、コンテナの起動失敗）で提出を
       落とさない。恒久的な失敗と区別し、回数の上限で打ち切る。
リース  ワーカーが死んだジョブを別のワーカーが引き取れる。取り込み中の
       ジョブが永久に RUNNING で残ると、その学習者だけ結果が返らない。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core import GradingPhase
from aijudge_core.ids import (
    GradingJobId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
    TenantId,
)

# 既定のリトライ上限。これを超えたら人間が見る。
DEFAULT_MAX_ATTEMPTS = 3
# 指数バックオフの基準。締切前のバーストで再試行が固まらないよう、
# 呼び出し側が科目ごとに変えられるようにしてある。
DEFAULT_BACKOFF_SECONDS = 30.0
# ワーカーがジョブを保持できる時間。超えたら死んだと見なして再割り当てする。
DEFAULT_LEASE_SECONDS = 900.0


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    # リトライ上限に達した。**採点は失敗のまま残す。**
    # 0 点にはしない（採点できないのは学習者の責任ではない）。
    FAILED = "failed"
    # 提出が取り下げられた、課題が消えた等で採点する意味が無くなった。
    CANCELLED = "cancelled"


class JobReason(StrEnum):
    """なぜこのジョブが要るのか。冪等キーの一部になる。"""

    SUBMISSION = "submission"
    """新しい提出。"""

    REGRADE = "regrade"
    """再採点。モデル更新や観点の修正のあとに全件流し直す用途。"""


class GradingJob(BaseModel):
    """採点 1 回分のジョブ。

    状態遷移はメソッドで行い、新しい値を返す（この型は不変）。
    どこからでも state を書き換えられると、リースやリトライ回数の不整合が
    静かに入り込む。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: GradingJobId
    tenant_id: TenantId
    submission_id: SubmissionId
    task_version_id: TaskVersionId
    subject_profile: str = Field(min_length=1)
    reason: JobReason = JobReason.SUBMISSION
    # どの段階か。速い段階と遅い段階を別のジョブにして、ワーカーを分けられる
    # ようにする（`GradingPhase` 参照）。
    phase: GradingPhase = GradingPhase.DETERMINISTIC
    # AI 段階が土台にする決定的評価の結果。DETERMINISTIC では None。
    base_run_id: GradingRunId | None = None
    # 同じキーのジョブは 1 つしか作らない。再投入は既存のジョブを返す。
    idempotency_key: str = Field(min_length=1)
    state: JobState = JobState.QUEUED
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1)
    # この時刻まで実行しない。バックオフに使う。
    available_at: datetime
    # RUNNING のとき、この時刻を過ぎたらワーカーが死んだと見なす。
    lease_expires_at: datetime | None = None
    worker: str | None = None
    last_error: str | None = None
    grading_run_id: GradingRunId | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        if self.state is JobState.RUNNING and self.lease_expires_at is None:
            raise ValueError("a running job must hold a lease")
        if self.state is not JobState.RUNNING and self.lease_expires_at is not None:
            raise ValueError("only a running job may hold a lease")
        if self.state is JobState.DONE and self.grading_run_id is None:
            raise ValueError("a completed job must reference its GradingRun")
        if self.state is JobState.FAILED and not self.last_error:
            raise ValueError("a failed job must carry the error that stopped it")
        if self.phase is GradingPhase.AI and self.base_run_id is None:
            # AI 段階は決定的評価の結果の上に積む。土台が無いのに走らせると、
            # サンドボックスをもう一度回すことになる（費用も結果も変わる）。
            raise ValueError("an ai-phase job must name the run it builds on")
        if self.phase is GradingPhase.DETERMINISTIC and self.base_run_id is not None:
            raise ValueError("a deterministic-phase job builds on nothing")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def is_available(self, now: datetime) -> bool:
        """いま実行してよいか。

        リースが切れた RUNNING も対象に含める。ワーカーが死んだまま
        RUNNING で残ったジョブを誰も拾わないと、その学習者だけ
        結果が返らないまま放置される。
        """
        if self.state is JobState.QUEUED:
            return self.available_at <= now
        if self.state is JobState.RUNNING:
            return self.lease_expires_at is not None and self.lease_expires_at <= now
        return False

    # -- 遷移 --------------------------------------------------------------

    def reserved(
        self, now: datetime, *, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> GradingJob:
        """ワーカーが取る。試行回数はここで増やす。

        成功時ではなく取得時に増やすのが要点。ワーカーが採点中に落ちると
        成功も失敗も記録されないので、成功時に数えていると同じジョブを
        無限に取り直す（プロセスを殺すような提出でそれが起きる）。
        """
        if self.terminal:
            raise ValueError(f"cannot reserve a {self.state} job")
        return self.model_copy(
            update={
                "state": JobState.RUNNING,
                "attempts": self.attempts + 1,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "worker": worker,
                "updated_at": now,
            }
        )

    def completed(self, now: datetime, grading_run_id: GradingRunId) -> GradingJob:
        return self.model_copy(
            update={
                "state": JobState.DONE,
                "lease_expires_at": None,
                "worker": None,
                "grading_run_id": grading_run_id,
                "last_error": None,
                "updated_at": now,
            }
        )

    def failed(
        self,
        now: datetime,
        error: str,
        *,
        permanent: bool = False,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> GradingJob:
        """失敗を記録する。上限に達していなければ QUEUED へ戻す。

        `permanent=True` は「何度やっても同じ」失敗（課題定義が壊れている、
        科目プロファイルに存在しない評価器が書いてある）。リトライしても
        GPU を無駄に回すだけなので即座に打ち切る。
        """
        exhausted = permanent or self.attempts >= self.max_attempts
        if exhausted:
            return self.model_copy(
                update={
                    "state": JobState.FAILED,
                    "lease_expires_at": None,
                    "worker": None,
                    "last_error": error,
                    "updated_at": now,
                }
            )
        # 指数バックオフ。1 回目 30 秒、2 回目 60 秒…
        delay = backoff_seconds * (2 ** (self.attempts - 1)) if self.attempts else backoff_seconds
        return self.model_copy(
            update={
                "state": JobState.QUEUED,
                "lease_expires_at": None,
                "worker": None,
                "last_error": error,
                "available_at": now + timedelta(seconds=delay),
                "updated_at": now,
            }
        )

    def cancelled(self, now: datetime, reason: str) -> GradingJob:
        return self.model_copy(
            update={
                "state": JobState.CANCELLED,
                "lease_expires_at": None,
                "worker": None,
                "last_error": reason,
                "updated_at": now,
            }
        )


def job_idempotency_key(
    submission_id: SubmissionId,
    reason: JobReason,
    *,
    discriminator: str = "",
    phase: GradingPhase = GradingPhase.DETERMINISTIC,
) -> str:
    """ジョブの冪等キー。

    提出 1 件につき、理由と段階ごとに 1 ジョブ。段階をキーに入れるのは、
    決定的評価と AI 評価が別ジョブだから ── 入れないと、AI 段階の投入が
    決定的段階の既存ジョブに吸収されて永久に走らない。

    再採点は `discriminator` にモデル版やプロンプト版を入れて区別する
    （同じモデルで二度流し直してもジョブは増えない）。
    """
    parts = [str(submission_id), reason.value]
    if discriminator:
        parts.append(discriminator)
    if phase is not GradingPhase.DETERMINISTIC:
        # 既定の段階だけキーに出さない。既存のキーと互換にしておくと、
        # 移行中のキューに二重投入が起きない。
        parts.append(phase.value)
    return "|".join(parts)
