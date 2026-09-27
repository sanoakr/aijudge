"""インメモリ実装と保存層の実装が、それぞれの Protocol を満たす（#435）。

`ReviewRepository` は `@runtime_checkable` なのに、インメモリ実装は 3 つの
メソッドを持たず `isinstance` が偽だった。画面は SQL の具象メソッドを直接
呼び、Protocol は実態を言っていなかった。ここで両方を突き合わせる。

課題・KC も同じだった（段階 2-1）: 課題の削除・埋め込み、KC の保存・削除・
一覧は SQL にだけあり、どの Protocol も言っていなかった。
"""

from __future__ import annotations

import pytest

from aijudge_audit import AuditLog, InMemoryAuditLog
from aijudge_authoring import InMemoryTaskRepository, TaskRepository, TaskStore, TaskUsageQueries
from aijudge_ide import (
    ActivityIndex,
    BufferStore,
    InMemoryActivityIndex,
    InMemoryBufferStore,
    InMemoryRunQueue,
    InMemorySubmissionLinkStore,
    RunQueue,
    SubmissionLinkStore,
)
from aijudge_identity import IdentityRepository, InMemoryIdentityRepository
from aijudge_persistence import Database
from aijudge_skill import InMemorySkillRepository, SkillRepository
from aijudge_submission import (
    ArtifactStore,
    CourseReviewQueries,
    GradingRunRepository,
    JobQueue,
    Outbox,
    ReviewRepository,
    ReviewStore,
    SubmissionRepository,
)
from aijudge_submission.memory import (
    InMemoryArtifactStore,
    InMemoryGradingRunRepository,
    InMemoryJobQueue,
    InMemoryOutbox,
    InMemoryReviewRepository,
    InMemorySubmissionRepository,
)

PAIRS = [
    (SubmissionRepository, InMemorySubmissionRepository, "submissions"),
    (GradingRunRepository, InMemoryGradingRunRepository, "runs"),
    (ReviewRepository, InMemoryReviewRepository, "reviews"),
    (JobQueue, InMemoryJobQueue, "jobs"),
    (Outbox, InMemoryOutbox, "outbox"),
    (TaskRepository, InMemoryTaskRepository, "tasks"),
    (IdentityRepository, InMemoryIdentityRepository, "identity"),
    (SkillRepository, InMemorySkillRepository, "skills"),
    (AuditLog, InMemoryAuditLog, "audit"),
    (RunQueue, InMemoryRunQueue, "run_requests"),
    (BufferStore, InMemoryBufferStore, "ide_buffers"),
    (SubmissionLinkStore, InMemorySubmissionLinkStore, "ide_links"),
    (ActivityIndex, InMemoryActivityIndex, "ide_activity"),
]


@pytest.mark.parametrize(("protocol", "memory", "_attr"), PAIRS)
def test_the_in_memory_store_keeps_its_protocol(protocol, memory, _attr) -> None:
    assert isinstance(memory(), protocol), f"{memory.__name__} does not satisfy {protocol.__name__}"


def test_the_in_memory_artifact_store_keeps_its_protocol() -> None:
    assert isinstance(InMemoryArtifactStore(), ArtifactStore)


@pytest.mark.parametrize(("protocol", "_memory", "attr"), PAIRS)
def test_the_sql_store_keeps_its_protocol(protocol, _memory, attr) -> None:
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            assert isinstance(getattr(uow, attr), protocol)
    finally:
        database.dispose()


def test_only_the_sql_reviews_offer_the_course_queries() -> None:
    """コース単位の読み取りは保存層だけ。**インメモリが持たないことも固定する**
    ── 持っているふりをすると、課題を知らないまま空を返す実装が紛れ込む。
    """
    assert not isinstance(InMemoryReviewRepository(), CourseReviewQueries)
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            assert isinstance(uow.reviews, ReviewStore)
    finally:
        database.dispose()


def test_only_the_sql_tasks_count_their_use() -> None:
    """提出の件数と通過率は保存層だけ。**インメモリが「0 件」と答えられないことを
    固定する** ── 答えられると、提出のある課題を消せることになる。
    """
    assert not isinstance(InMemoryTaskRepository(), TaskUsageQueries)
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            assert isinstance(uow.tasks, TaskStore)
    finally:
        database.dispose()
