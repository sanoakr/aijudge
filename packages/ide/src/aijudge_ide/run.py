"""試しの実行の要求（ADR 0024）。

学習者が IDE の「実行」を押すと 1 件できる。runner が取り、sandbox で動かし、
結果を同じ行に書き戻す。**採点ジョブではない** ── 成績の根拠にならず、
`GradingRun` も作らず、終わった行はしばらくで消す（`RunQueue.purge_finished`）。

採点ジョブ（`aijudge_submission.GradingJob`）と形は似ているが、性質が逆である。

| | 採点ジョブ | 試しの実行 |
|---|---|---|
| 失敗したら | 再試行する（提出を落とさない） | 再試行しない（学習者が押し直す） |
| 古くなったら | 古くならない | 30 秒で無意味（とうに書き換えている） |
| 結果 | 追記で永久に残す（P8） | 画面に出したら用済み |

だから再試行の回数もバックオフも持たない。持たせると、学習者が書き換えた
あとの古いコードが、忘れた頃に走る。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import NewType, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core.ids import TaskVersionId, TenantId, UserId

RunRequestId = NewType("RunRequestId", str)

# ソースと標準入力の上限（設計書 §8.1）。**文字数ではなく UTF-8 のバイト数**で
# 数える ── 日本語のコメントは 1 文字 3 バイトで、sandbox に書くのはバイト列である。
MAX_SOURCE_BYTES = 64 * 1024
MAX_STDIN_BYTES = 64 * 1024

# 画面に出す出力の上限（文字数）。sandbox は 1 MiB で切るが、それを行に
# 丸ごと持つと、無限に printf する提出 1 件で 1 MiB の JSON が DB に入り、
# 0.5 秒ごとの問い合わせがそれを毎回読む。先頭だけで学習者には足りる。
DISPLAY_OUTPUT_CHARS = 64 * 1024

# 待っている要求をこれ以上経ったら実行しない（ADR 0024 §1）。
STALE_AFTER_SECONDS = 30.0

# runner が要求を握っていられる時間。過ぎたら runner が死んだと見なして
# 失敗にする。**要求 1 件の最長より長く取る** ── コンパイルの上限（既定 30 秒）
# と実行の上限（既定 5 秒）に、コンテナの起動を足しても十分に収まる値。
# 短すぎると、生きている runner の結果を「失敗」で上書きしてしまう。
DEFAULT_LEASE_SECONDS = 120.0

# 同じ学習者の連続実行の間隔（秒、設計書 §8.1）。
DEFAULT_COOLDOWN_SECONDS = 3.0

# リースが切れた要求に付ける理由。**学習者に見せる文言**なので、どの runner が
# どう落ちたかは書かない（運用ログの側に出る）。両方の実装が同じ文言を使う。
RUNNER_LOST = "実行環境が応答しなくなりました。もう一度実行してください。"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    # 待っているうちに古くなった。**実行していない。**
    EXPIRED = "expired"
    # 実行できなかった（課題が実行に対応していない、実行環境が無い、runner が
    # 途中で死んだ）。**学習者のコードが落ちたのはこれではない** ── それは
    # DONE で、終了コードや時間切れとして結果に載る。
    FAILED = "failed"

    @property
    def in_flight(self) -> bool:
        """まだ終わっていないか。1 人が同時に持てるのはこの状態の要求 1 件だけ。"""
        return self in (RunState.QUEUED, RunState.RUNNING)


IN_FLIGHT_STATES: tuple[RunState, ...] = (RunState.QUEUED, RunState.RUNNING)
TERMINAL_STATES: tuple[RunState, ...] = (RunState.DONE, RunState.EXPIRED, RunState.FAILED)


class RunStage(StrEnum):
    """結果がどの段階のものか。

    C のコンパイルエラーは「実行して落ちた」とは違う。画面はこれで出し分ける
    （コンパイラの出力を見せるか、プログラムの出力を見せるか）。
    """

    COMPILE = "compile"
    RUN = "run"


class RunOutcome(BaseModel):
    """1 回の実行の結果。画面に出す分だけを持つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: RunStage
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(default=0, ge=0)
    timed_out: bool = False
    signal_name: str | None = None
    # sandbox の上限か、画面の上限（`DISPLAY_OUTPUT_CHARS`）で切ったか。
    truncated: bool = False
    # どの隔離で動いたか（`aijudge_sandbox.Isolation` の値）。ide は sandbox を
    # import しないので文字列で持つ。
    isolation: str = ""


class RunRequest(BaseModel):
    """試しの実行 1 件。

    状態遷移はメソッドで行い、新しい値を返す（この型は不変）。採点ジョブと
    同じ作り ── どこからでも state を書き換えられると、リースの不整合が
    静かに入り込む。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: RunRequestId
    tenant_id: TenantId
    learner_id: UserId
    task_version_id: TaskVersionId
    # どの IDE の画面から来たか。行動記録（段階 3）と突き合わせる。
    ide_session_id: str | None = None
    source: str
    # 自由入力の標準入力。**サンプルで実行するときは空**で、中身は runner が
    # 課題から引く（学習者から届いた「サンプルの中身」を信じない）。
    stdin: str = ""
    # 公開サンプルの名前（`TestCase.name`）。非公開のケースは runner が断る。
    sample_name: str | None = None
    state: RunState = RunState.QUEUED
    worker: str | None = None
    lease_expires_at: datetime | None = None
    outcome: RunOutcome | None = None
    # FAILED の理由。**学習者にそのまま見せる文言**なので、内部の例外文を
    # 入れない（パスや設定値が漏れる）。
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        if self.sample_name is not None and self.stdin:
            raise ValueError("a run uses either a sample or free stdin, not both")
        if self.state is RunState.RUNNING and self.lease_expires_at is None:
            raise ValueError("a running request must hold a lease")
        if self.state is not RunState.RUNNING and self.lease_expires_at is not None:
            raise ValueError("only a running request may hold a lease")
        if self.state is RunState.DONE and self.outcome is None:
            raise ValueError("a finished request must carry its outcome")
        if self.state is RunState.FAILED and not self.error:
            raise ValueError("a failed request must say why")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_stale(self, now: datetime, *, stale_after: float = STALE_AFTER_SECONDS) -> bool:
        """待ったまま古くなったか。走っている要求は古くならない（リースで見る）。"""
        return (
            self.state is RunState.QUEUED
            and self.created_at + timedelta(seconds=stale_after) <= now
        )

    def lease_lost(self, now: datetime) -> bool:
        """runner が握ったまま戻ってこないか。"""
        return (
            self.state is RunState.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at <= now
        )

    # -- 遷移 --------------------------------------------------------------

    def reserved(
        self, now: datetime, *, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> RunRequest:
        if self.state is not RunState.QUEUED:
            raise ValueError(f"cannot reserve a {self.state} request")
        return self.model_copy(
            update={
                "state": RunState.RUNNING,
                "worker": worker,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "started_at": now,
                "updated_at": now,
            }
        )

    def completed(self, now: datetime, outcome: RunOutcome) -> RunRequest:
        if self.state is not RunState.RUNNING:
            raise ValueError(f"cannot complete a {self.state} request")
        return self._finished(now, state=RunState.DONE, outcome=outcome)

    def failed(self, now: datetime, error: str) -> RunRequest:
        if self.terminal:
            raise ValueError(f"cannot fail a {self.state} request")
        return self._finished(now, state=RunState.FAILED, error=error)

    def expired(self, now: datetime) -> RunRequest:
        if self.state is not RunState.QUEUED:
            raise ValueError(f"cannot expire a {self.state} request")
        return self._finished(now, state=RunState.EXPIRED)

    def _finished(
        self,
        now: datetime,
        *,
        state: RunState,
        outcome: RunOutcome | None = None,
        error: str | None = None,
    ) -> RunRequest:
        return self.model_copy(
            update={
                "state": state,
                "worker": None,
                "lease_expires_at": None,
                "outcome": outcome,
                "error": error,
                "finished_at": now,
                "updated_at": now,
            }
        )


def clip_for_display(text: str, limit: int = DISPLAY_OUTPUT_CHARS) -> tuple[str, bool]:
    """画面に出す分だけ残す。切ったかどうかも返す。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True
