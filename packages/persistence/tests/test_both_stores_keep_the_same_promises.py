"""インメモリ実装と保存層の実装が、それぞれの Protocol を満たす（#435）。

`ReviewRepository` は `@runtime_checkable` なのに、インメモリ実装は 3 つの
メソッドを持たず `isinstance` が偽だった。画面は SQL の具象メソッドを直接
呼び、Protocol は実態を言っていなかった。ここで両方を突き合わせる。
"""

from __future__ import annotations

import pytest

from aijudge_persistence import Database
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
