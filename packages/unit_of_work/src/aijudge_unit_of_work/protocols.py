"""13 のリポジトリを持つ UnitOfWork の Protocol。

リポジトリは**読み取り専用のプロパティ**で宣言する。属性として書くと、
実装が Protocol より狭い型（`SqlTaskRepository`）を持つことを型検査が
許さない（書き込める属性は不変なので）。
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from aijudge_audit import AuditLog
from aijudge_authoring import TaskRepository, TaskStore
from aijudge_ide import ActivityIndex, BufferStore, RunQueue, SubmissionLinkStore
from aijudge_identity import IdentityRepository
from aijudge_skill import SkillRepository
from aijudge_submission import (
    GradingRunRepository,
    JobQueue,
    Outbox,
    ReviewRepository,
    ReviewStore,
    SubmissionRepository,
)


@runtime_checkable
class UnitOfWork(Protocol):
    """1 つのトランザクション境界と、その上の全リポジトリ。

    **型はインメモリでも満たせる側で書く。** 課題の表との結合でしか答えられない
    読み取り（コース単位のレビュー、課題の利用状況）は `StoreUnitOfWork` にだけ
    ある ── ここに入れると、インメモリの実装は空を返すしかなくなる（#464）。

    **監査（`audit`）が同じ口にあるのが要点**（ADR 0016）。操作が巻き戻れば
    監査行も巻き戻り、監査行が書けなければ操作も成立しない。
    """

    @property
    def submissions(self) -> SubmissionRepository: ...

    @property
    def runs(self) -> GradingRunRepository: ...

    @property
    def reviews(self) -> ReviewRepository: ...

    @property
    def jobs(self) -> JobQueue: ...

    @property
    def outbox(self) -> Outbox: ...

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def identity(self) -> IdentityRepository: ...

    @property
    def skills(self) -> SkillRepository: ...

    @property
    def audit(self) -> AuditLog: ...

    @property
    def run_requests(self) -> RunQueue:
        """ブラウザ IDE の試しの実行（ADR 0024）。**採点キュー（`jobs`）とは別。**"""
        ...

    @property
    def ide_buffers(self) -> BufferStore: ...

    @property
    def ide_links(self) -> SubmissionLinkStore: ...

    @property
    def ide_activity(self) -> ActivityIndex: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class StoreUnitOfWork(UnitOfWork, Protocol):
    """保存層の UnitOfWork。表の結合で答える読み取りも持つ。

    コース単位のレビューの読み取り（`CourseReviewQueries`）と課題の利用状況
    （`TaskUsageQueries`）は、提出・採点・課題の表を突き合わせないと答えられない。
    """

    @property
    def reviews(self) -> ReviewStore: ...

    @property
    def tasks(self) -> TaskStore: ...
