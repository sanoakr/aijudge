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


def test_all_learners_of_a_task_come_back(database: Database) -> None:
    """受付終了時の自動提出が読む（設計書 §9.1）。他の課題は混ざらない。"""
    for store in _both(database):
        store.save(buffer("mine", learner=LEARNER, task=P1))
        store.save(buffer("theirs", learner=OTHER, task=P1))
        store.save(buffer("other task", learner=LEARNER, task=P2))

        found = store.for_task(P1)

        assert sorted(b.source for b in found) == ["mine", "theirs"]
        assert store.task_ids() == (P1, P2)


# -- 提出の出どころ ----------------------------------------------------------

from aijudge_core.ids import SubmissionId  # noqa: E402
from aijudge_ide import (  # noqa: E402
    InMemorySubmissionLinkStore,
    SubmissionLink,
    SubmissionLinkStore,
    SubmissionOrigin,
)

SUB = SubmissionId("sub_" + "5" * 32)


def _links(database: Database) -> Iterator[SubmissionLinkStore]:
    yield InMemorySubmissionLinkStore()
    with database.unit_of_work() as uow:
        yield uow.ide_links
        uow.commit()


def a_link(origin: SubmissionOrigin) -> SubmissionLink:
    return SubmissionLink(
        submission_id=SUB,
        tenant_id=TENANT,
        learner_id=LEARNER,
        task_id=P1,
        origin=origin,
        content_hash=content_hash("x"),
        recorded_at=NOW,
    )


def test_a_link_reads_back_whole(database: Database) -> None:
    for links in _links(database):
        link = a_link(SubmissionOrigin.EDITOR)
        links.record(link)
        assert links.for_submission(SUB) == link
        assert links.for_submission(SubmissionId("sub_" + "6" * 32)) is None


def test_the_first_origin_wins(database: Database) -> None:
    """本人が押した提出を、あとの自動提出が同じ内容で「自動」に書き換えない。"""
    for links in _links(database):
        links.record(a_link(SubmissionOrigin.EDITOR))
        links.record(a_link(SubmissionOrigin.AUTO_CLOSE))
        found = links.for_submission(SUB)
        assert found is not None and found.origin is SubmissionOrigin.EDITOR
