"""IDE の自動保存（設計書 §6.5）。

学習者の各タブの最新の内容を、**(学習者, 課題) ごとに 1 件だけ**、上書きで
持つ。リロードしても、PC を替えても続きから書ける。

**提出ではない。** 採点は提出だけを読み、ここは読まない。受付終了時の自動
提出（§9.1、段階 3）はここから最新の内容を取るが、そのときも既存の提出の
経路（`SubmissionService.accept`）を通す。

**行動記録でもない**（ADR 0023）。あちらは追記で経過を残し、こちらは最新だけを
残す。ADR 0019 の `task_drafts`（承認待ちの課題）とも無関係なので、名前に
`draft` は使わない。

鍵を課題版ではなく**課題**にしてあるのは、教員が誤字を直して版が上がっても、
学習者の書きかけが消えないようにするため。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core.ids import TaskId, TenantId, UserId

from .run import MAX_SOURCE_BYTES


class IdeBuffer(BaseModel):
    """1 人の 1 課題の、いまの内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    learner_id: UserId
    task_id: TaskId
    # どの形式で書いているか（`EDITOR_FORMATS` の鍵）。課題が複数の形式を
    # 受け付けるとき、リロードしても選んだ形式に戻れるように持つ。
    suffix: str = Field(min_length=2, max_length=8)
    source: str
    updated_at: datetime
    # 中身の SHA-256。提出との突き合わせ（前回の提出から変わったか）と、
    # 同じ内容の保存を書き込まずに済ませるために持つ。
    content_hash: str = Field(min_length=64, max_length=64)


def content_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class BufferTooLarge(ValueError):
    """実行と同じ上限（`MAX_SOURCE_BYTES`）を超えた。"""


def make_buffer(
    *,
    tenant_id: TenantId,
    learner_id: UserId,
    task_id: TaskId,
    suffix: str,
    source: str,
    now: datetime,
) -> IdeBuffer:
    """保存する 1 件を作る。大きすぎれば `BufferTooLarge`。

    上限を実行と揃えるのは、保存できたのに実行も提出もできない内容を
    作らせないため。
    """
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise BufferTooLarge(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    return IdeBuffer(
        tenant_id=tenant_id,
        learner_id=learner_id,
        task_id=task_id,
        suffix=suffix,
        source=source,
        updated_at=now,
        content_hash=content_hash(source),
    )


@runtime_checkable
class BufferStore(Protocol):
    """自動保存の置き場。インメモリと SQL の 2 実装に同じテストを当てる。"""

    def save(self, buffer: IdeBuffer) -> None:
        """上書きで保存する（無ければ作る）。commit はしない。"""
        ...

    def get(self, learner_id: UserId, task_id: TaskId) -> IdeBuffer | None: ...

    def for_tasks(self, learner_id: UserId, task_ids: list[TaskId]) -> dict[TaskId, IdeBuffer]:
        """IDE の画面を開いたときに、全タブの内容をまとめて引く。"""
        ...

    def for_task(self, task_id: TaskId) -> tuple[IdeBuffer, ...]:
        """この課題の全員分。**受付終了時の自動提出だけが使う**（設計書 §9.1）。"""
        ...

    def task_ids(self) -> tuple[TaskId, ...]:
        """自動保存のある課題。自動提出はここから辿る（全コースを舐めない）。"""
        ...
