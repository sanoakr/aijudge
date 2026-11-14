"""受付の補いと冪等キーの範囲を、本物のトランザクションで確かめる（#686）。

インメモリの `commit` は回数を数えるだけで、抜けても何も巻き戻らない。
**補ったジョブが残るかどうかは SQL でしか分からない。** 補いの後の commit が
落ちる（あるいは return の後ろへ移る）と、`with` を抜けた時点でロールバック
され、「提出は残ったがジョブが無い」提出は再送しても永久に未採点のまま
になる ── `accept` は job を返すので画面は正常に見える。

あわせて、冪等キーが課題版とテナントをまたがないことを SQL 側でも見る。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from aijudge_core import ArtifactKind
from aijudge_core.ids import SubmissionId, TaskVersionId, TenantId, UserId
from aijudge_persistence import Database
from aijudge_persistence.schema import GradingJobRow, SubmissionKeyRow
from aijudge_submission import (
    FilesystemArtifactStore,
    IncomingFile,
    InMemoryArtifactStore,
    JobReason,
    SubmissionService,
    job_idempotency_key,
)

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "8" * 32)
TASK = TaskVersionId("tsv_" + "3" * 32)
REVISED_TASK = TaskVersionId("tsv_" + "5" * 32)
LEARNER = UserId("usr_" + "4" * 32)
PROFILE = "cs_lang_c_intro"
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Iterator[Database]:
    """`test_repositories.py` と同じ。PostgreSQL では毎テストで作り直す。"""
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


@pytest.fixture
def service(database: Database, tmp_path: Path) -> SubmissionService:
    return SubmissionService(
        database.unit_of_work,
        InMemoryArtifactStore(),
        stream_store=FilesystemArtifactStore(tmp_path / "video"),
        clock=lambda: NOW,
    )


def template() -> list[IncomingFile]:
    return [IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(void){}")]


def accept(service: SubmissionService, **overrides):
    kwargs = {
        "tenant_id": TENANT,
        "task_version_id": TASK,
        "learner_id": LEARNER,
        "subject_profile": PROFILE,
        "files": template(),
    }
    kwargs.update(overrides)
    return service.accept(**kwargs)


def stream(service: SubmissionService, idempotency_key: str | None = None):
    return service.accept_stream(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile=PROFILE,
        filename="demo.mp4",
        kind=ArtifactKind.VIDEO,
        chunks=iter([b"same", b"bytes"]),
        max_bytes=1_000_000,
        idempotency_key=idempotency_key,
    )


def lose_every_job(database: Database) -> None:
    """前回の投入がジョブを積む直前で落ちた状態を作る（提出と鍵は残る）。"""
    with database.session() as session:
        session.execute(delete(GradingJobRow))
        session.commit()


def stored_job(database: Database, submission_id: SubmissionId):
    """別の UnitOfWork から読む ── 書いた側のセッションの中身は見ない。"""
    with database.unit_of_work() as uow:
        return uow.jobs.find_by_idempotency_key(
            job_idempotency_key(submission_id, JobReason.SUBMISSION)
        )


# --------------------------------------------------------------------------
# 補ったジョブは残る
# --------------------------------------------------------------------------


def test_a_job_repaired_on_resubmission_is_still_there_afterwards(
    database: Database, service: SubmissionService
) -> None:
    first = accept(service)
    lose_every_job(database)
    assert stored_job(database, first.submission.id) is None

    second = accept(service)

    assert second.deduplicated
    repaired = stored_job(database, first.submission.id)
    assert repaired is not None
    assert repaired.id == second.job.id


def test_a_video_job_repaired_on_resubmission_is_still_there_afterwards(
    database: Database, service: SubmissionService
) -> None:
    """動画経路の補い（`_existing_for_key`）も同じ。

    明示キー（分割アップロードの upload_id と同じ役目）で送る。キーを送らない
    経路は、下の xfail のとおり PostgreSQL では提出そのものが通らない。
    """
    first = stream(service, idempotency_key="upl_resend")
    lose_every_job(database)

    again = stream(service, idempotency_key="upl_resend")

    assert again.deduplicated
    repaired = stored_job(database, first.submission.id)
    assert repaired is not None
    assert repaired.id == again.job.id


# --------------------------------------------------------------------------
# 冪等キーの範囲
# --------------------------------------------------------------------------


def test_the_same_content_to_two_task_versions_is_two_submissions(
    database: Database, service: SubmissionService
) -> None:
    first = accept(service, task_version_id=TASK)
    second = accept(service, task_version_id=REVISED_TASK)

    assert not second.deduplicated
    assert second.submission.id != first.submission.id
    assert stored_job(database, second.submission.id) is not None


def test_an_explicit_key_does_not_reach_across_tenants(
    database: Database, service: SubmissionService
) -> None:
    mine = accept(service, idempotency_key="req-1")
    theirs = accept(service, tenant_id=OTHER_TENANT, idempotency_key="req-1")

    assert not theirs.deduplicated
    assert theirs.submission.id != mine.submission.id
    with database.unit_of_work() as uow:
        assert uow.submissions.find_by_idempotency_key(TENANT, "req-1").id == mine.submission.id
        assert (
            uow.submissions.find_by_idempotency_key(OTHER_TENANT, "req-1").id
            == theirs.submission.id
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "record_streamed の内容ベースのキー（intake.py:381、"
        "sha256:<64>:<課題版>:<学習者> で 145 文字）が "
        "submission_keys.idempotency_key の VARCHAR(128)（schema.py:138）を超える。"
        "PostgreSQL では Idempotency-Key 無しの POST /submit-video が DataError で落ちる"
    ),
)
def test_a_video_sent_without_a_key_fits_the_key_column(
    database: Database, service: SubmissionService
) -> None:
    """Idempotency-Key を送らない動画提出も、鍵の列に収まる。

    SQLite は VARCHAR の長さを強制しないので、保存できても長さを別に確かめる
    （`audit_events.target_id` と同じ見逃し方をしないため）。
    """
    stream(service)

    limit = SubmissionKeyRow.__table__.c.idempotency_key.type.length
    with database.session() as session:
        keys = session.execute(select(SubmissionKeyRow.idempotency_key)).scalars().all()
    assert keys
    assert all(len(key) <= limit for key in keys)
