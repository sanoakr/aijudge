"""IDE からの提出の出どころ（設計書 §9）。

**`Submission` には何も足さない**（不変条件 I1）。採点は提出がどこから来たかを
知らず、知る必要もない（I2）。それでも「本人が押した提出」と「受付終了時に
サーバが出した提出」は後から区別できなければならない ── 自動提出された版は
本人が出すつもりだったものとは限らず、問い合わせに答えるときに要る。

そこで提出の外に 1 行ずつ残す。行が無い提出は、ファイルでの提出（`/submit`）で
ある。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import SubmissionId, TaskId, TenantId, UserId


class SubmissionOrigin(StrEnum):
    # 学習者がエディタの「提出」を押した。
    EDITOR = "editor"
    # 受付の終わりに、サーバが自動保存の最新を出した（§9.1）。
    AUTO_CLOSE = "auto_close"


class SubmissionLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    submission_id: SubmissionId
    tenant_id: TenantId
    learner_id: UserId
    task_id: TaskId
    origin: SubmissionOrigin
    # 提出した内容の SHA-256（`aijudge_ide.content_hash`）。
    content_hash: str = Field(min_length=64, max_length=64)
    ide_session_id: str | None = None
    recorded_at: datetime


@runtime_checkable
class SubmissionLinkStore(Protocol):
    def record(self, link: SubmissionLink) -> None:
        """1 件残す。**同じ提出に 2 度目は残さない**（最初の出どころが正しい ──
        本人が押した提出を、あとの自動提出が同じ内容で「自動」に書き換えない）。"""
        ...

    def for_submission(self, submission_id: SubmissionId) -> SubmissionLink | None: ...
