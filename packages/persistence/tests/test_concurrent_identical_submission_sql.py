"""同じ内容の提出が同時に届いたときの受け側を、SQL の一意制約で確かめる（2026-11-14・#679）。

インメモリでの同じ確認は `packages/submission/tests/test_concurrent_identical_submission.py`。
ここでは本物の一意制約（`submission_keys` の主キー）が止めたとき、生の `IntegrityError` が
上がらず、後から来た側が先に成立した提出を返すことを確かめる。以前は `execute` の autoflush が
`try` の外で違反を出し、例外の型が揃っていなかった。

PostgreSQL では 2 本目の INSERT が 1 本目の commit を待ってから違反になる。ここでは
「両方が『まだ無い』を見た」形を、1 回目の検索だけ無いと答えさせて作る。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from aijudge_core import ArtifactKind
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_persistence import Base, Database
from aijudge_persistence.repositories import SqlSubmissionRepository
from aijudge_submission import IncomingFile, InMemoryArtifactStore, SubmissionService

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Database:
    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def _accept(service: SubmissionService):
    return service.accept(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}\n")],
    )


def test_the_loser_of_a_unique_key_race_gets_the_winning_submission(
    database: Database, monkeypatch
) -> None:
    store = InMemoryArtifactStore()
    service = SubmissionService(database.unit_of_work, store, clock=lambda: NOW)
    winner = _accept(service)
    before = set(store._blobs)

    real = SqlSubmissionRepository.find_by_idempotency_key
    calls = {"n": 0}

    def once_blind(self, tenant_id, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(self, tenant_id, key)

    monkeypatch.setattr(SqlSubmissionRepository, "find_by_idempotency_key", once_blind)

    loser = _accept(service)

    assert loser.deduplicated
    assert loser.submission.id == winner.submission.id
    assert loser.job.id == winner.job.id
    assert set(store._blobs) == before, "負けた側の成果物が残っている"
    with database.unit_of_work() as uow:
        assert len(uow.submissions.list_for_learner(TENANT, LEARNER, TASK)) == 1
        assert uow.jobs.pending_count() == 1
