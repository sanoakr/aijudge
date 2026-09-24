"""IDE の自動保存の 2 つの実装が同じ規則を守ること（設計書 §6.5）。

- (学習者, 課題) ごとに 1 件で、上書きする
- 他人の保存は見えない
- 全タブ分をまとめて引ける
- 上限は実行と同じ
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import TaskId, TenantId, UserId
from aijudge_ide import (
    MAX_SOURCE_BYTES,
    BufferStore,
    BufferTooLarge,
    InMemoryBufferStore,
    content_hash,
    make_buffer,
)
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
OTHER = UserId("usr_" + "2" * 32)
P1 = TaskId("tsk_" + "a" * 32)
P2 = TaskId("tsk_" + "b" * 32)
P3 = TaskId("tsk_" + "c" * 32)
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


def _both(database: Database) -> Iterator[BufferStore]:
    yield InMemoryBufferStore()
    with database.unit_of_work() as uow:
        yield uow.ide_buffers
        uow.commit()


def buffer(source: str, *, learner: UserId = LEARNER, task: TaskId = P1, at: datetime = NOW):
    return make_buffer(
        tenant_id=TENANT, learner_id=learner, task_id=task, suffix=".c", source=source, now=at
    )


def test_a_buffer_reads_back_whole(database: Database) -> None:
    for store in _both(database):
        saved = buffer("int main(void){}\n")
        store.save(saved)
        assert store.get(LEARNER, P1) == saved


def test_saving_again_overwrites(database: Database) -> None:
    for store in _both(database):
        store.save(buffer("v1"))
        later = buffer("v2", at=NOW + timedelta(seconds=10))
        store.save(later)

        found = store.get(LEARNER, P1)
        assert found == later
        assert found is not None and found.content_hash == content_hash("v2")


def test_another_learners_buffer_is_not_mine(database: Database) -> None:
    for store in _both(database):
        store.save(buffer("theirs", learner=OTHER))
        assert store.get(LEARNER, P1) is None


def test_all_tabs_come_back_together(database: Database) -> None:
    for store in _both(database):
        store.save(buffer("one", task=P1))
        store.save(buffer("two", task=P2))
        store.save(buffer("not mine", task=P3, learner=OTHER))

        found = store.for_tasks(LEARNER, [P1, P2, P3])

        assert {task: b.source for task, b in found.items()} == {P1: "one", P2: "two"}
        assert store.for_tasks(LEARNER, []) == {}


def test_the_limit_matches_running() -> None:
    """保存できたのに実行も提出もできない内容を作らせない。"""
    with pytest.raises(BufferTooLarge):
        buffer("a" * (MAX_SOURCE_BYTES + 1))
    buffer("a" * MAX_SOURCE_BYTES)
