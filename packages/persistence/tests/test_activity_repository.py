"""行動記録の索引の 2 つの実装が同じ規則を守ること（ADR 0023）。

- セッションは読み戻せる。告知の確認はコース単位で覚えている
- バッチは (セッション, seq) で 1 つ。再送は重複させない
- バッチは seq の順に返る（欠落は呼び出し側が数える）
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_ide import (
    ActivityIndex,
    EventBatch,
    IdeSession,
    IdeSessionId,
    InMemoryActivityIndex,
)
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
OTHER = UserId("usr_" + "2" * 32)
COURSE = CourseId("crs_" + "3" * 32)
OTHER_COURSE = CourseId("crs_" + "4" * 32)
SESSION = IdeSessionId("ide_" + "5" * 32)
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Iterator[Database]:
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def _both(database: Database) -> Iterator[ActivityIndex]:
    yield InMemoryActivityIndex()
    with database.unit_of_work() as uow:
        yield uow.ide_activity
        uow.commit()


def a_session(**update) -> IdeSession:
    base = {
        "id": SESSION,
        "tenant_id": TENANT,
        "learner_id": LEARNER,
        "course_id": COURSE,
        "unit": "第3回 配列",
        "started_at": NOW,
        "user_agent": "Mozilla/5.0",
        "consented_at": NOW,
    }
    return IdeSession(**base | update)


def a_batch(seq: int, **update) -> EventBatch:
    base = {
        "ide_session_id": SESSION,
        "seq": seq,
        "received_at": NOW + timedelta(seconds=10 * seq),
        "client_time": NOW + timedelta(seconds=10 * seq - 2),
        "event_count": 12,
        "snapshot_count": 0,
        "byte_size": 480,
        "sha256": "a" * 64,
        "path": f"crs/x/usr/ide/events/{seq:08d}.ndjson.gz",
    }
    return EventBatch(**base | update)


def test_a_session_reads_back_whole(database: Database) -> None:
    for index in _both(database):
        session = a_session()
        index.start_session(session)
        assert index.get_session(SESSION) == session
        assert index.get_session(IdeSessionId("ide_" + "6" * 32)) is None


def test_consent_is_remembered_per_course(database: Database) -> None:
    for index in _both(database):
        assert not index.has_consented(LEARNER, COURSE)
        index.start_session(a_session())
        assert index.has_consented(LEARNER, COURSE)
        assert not index.has_consented(LEARNER, OTHER_COURSE)
        assert not index.has_consented(OTHER, COURSE)


def test_a_resent_batch_is_not_duplicated(database: Database) -> None:
    """再送は日常である（通信の不調、`pagehide` の送り直し）。"""
    for index in _both(database):
        index.start_session(a_session())
        assert index.add_batch(a_batch(0)) is True
        assert index.add_batch(a_batch(0)) is False
        assert len(index.batches(SESSION)) == 1


def test_batches_come_back_in_seq_order_with_gaps_visible(database: Database) -> None:
    for index in _both(database):
        index.start_session(a_session())
        for seq in (2, 0, 3):
            index.add_batch(a_batch(seq))

        seqs = [batch.seq for batch in index.batches(SESSION)]

        # seq 1 が欠けていることが、呼び出し側から読める。
        assert seqs == [0, 2, 3]
        assert index.batches(SESSION)[0] == a_batch(0)
