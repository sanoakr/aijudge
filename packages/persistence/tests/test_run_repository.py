"""実行要求キューの 2 つの実装が同じ規則を守ること（ADR 0024）。

**同じテストを両方に当てる。** 片方だけ通る規則は、移行したときに初めて
破綻する。食い違ってよいのは行ロックだけである。

ここで確かめるのは:

- 1 人が同時に待てるのは 1 件（SQL では部分一意索引が止める）
- 取る順序と「前に何件」の数え方が同じ
- 古い要求の片付け（待ちすぎ → 期限切れ、リース切れ → 失敗）
- 終わった要求は消せる（結果は残さない）
- 採点キューとは混ざらない
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import SubmissionId, TaskVersionId, TenantId, UserId, new_id
from aijudge_ide import (
    RUNNER_LOST,
    InMemoryRunQueue,
    RunAlreadyPending,
    RunOutcome,
    RunQueue,
    RunRequest,
    RunRequestId,
    RunStage,
    RunState,
)
from aijudge_persistence import Database
from aijudge_submission import GradingJob, job_idempotency_key
from aijudge_submission.jobs import JobReason

TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
OTHER = UserId("usr_" + "2" * 32)
THIRD = UserId("usr_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
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


# 両方の実装に同じテストを当てるための入口。SQL 側は UnitOfWork の中でしか
# 使えないので、テスト本体は「開いたキュー」を受け取る（監査ログと同じ形）。
def _both(database: Database) -> Iterator[RunQueue]:
    yield InMemoryRunQueue()
    with database.unit_of_work() as uow:
        yield uow.run_requests
        uow.commit()


def a_request(
    *, learner: UserId = LEARNER, at: datetime = NOW, suffix: str | None = None
) -> RunRequest:
    return RunRequest(
        id=RunRequestId(f"run_{suffix * 32}" if suffix else new_id("run")),
        tenant_id=TENANT,
        learner_id=learner,
        task_version_id=VERSION,
        source="print(1)\n",
        stdin="",
        created_at=at,
        updated_at=at,
    )


def an_outcome() -> RunOutcome:
    return RunOutcome(stage=RunStage.RUN, exit_code=0, stdout="1\n", isolation="container")


def test_a_request_can_be_read_back_whole(database: Database) -> None:
    for queue in _both(database):
        request = a_request()
        queue.add(request)
        assert queue.get(request.id) == request


def test_a_learner_holds_one_request_in_flight(database: Database) -> None:
    """**2 件目は積めない。** SQL では検査を経ずに挿入しても索引が止める。"""
    for queue in _both(database):
        queue.add(a_request())
        with pytest.raises(RunAlreadyPending):
            queue.add(a_request(at=NOW + timedelta(seconds=5)))


def test_the_database_itself_refuses_a_second_request_in_flight(database: Database) -> None:
    """**最後に止めるのは DB である**（#146）。2 つのタブから同時に押すと、
    `in_flight_for` の検査はどちらも「無い」と答える。そこをすり抜けた 2 件目を
    部分一意索引が止めることを、別々のトランザクションで確かめる。
    """
    with database.unit_of_work() as uow:
        uow.run_requests.add(a_request())
        uow.commit()
    with database.unit_of_work() as uow, pytest.raises(RunAlreadyPending):
        uow.run_requests.add(a_request(at=NOW + timedelta(seconds=1)))


def test_finished_requests_do_not_block_the_next(database: Database) -> None:
    for queue in _both(database):
        first = a_request()
        queue.add(first)
        running = queue.reserve(NOW, worker="r1")
        assert running is not None
        queue.update(running.completed(NOW, an_outcome()))

        queue.add(a_request(at=NOW + timedelta(seconds=5)))
        assert queue.in_flight_for(LEARNER) is not None


def test_requests_are_taken_oldest_first(database: Database) -> None:
    for queue in _both(database):
        late = a_request(learner=OTHER, at=NOW + timedelta(seconds=2))
        early = a_request(learner=LEARNER, at=NOW)
        queue.add(late)
        queue.add(early)

        taken = queue.reserve(NOW + timedelta(seconds=3), worker="r1", lease_seconds=60)

        assert taken is not None and taken.id == early.id
        assert taken.state is RunState.RUNNING
        assert taken.worker == "r1"
        assert queue.get(early.id) == taken


def test_a_running_request_is_not_taken_twice(database: Database) -> None:
    for queue in _both(database):
        queue.add(a_request())
        assert queue.reserve(NOW, worker="r1") is not None
        assert queue.reserve(NOW, worker="r2") is None


def test_the_position_counts_who_is_ahead(database: Database) -> None:
    for queue in _both(database):
        first = a_request(learner=LEARNER, at=NOW, suffix="a")
        second = a_request(learner=OTHER, at=NOW + timedelta(seconds=1), suffix="b")
        # 同時刻は id の順（`reserve` と同じ）。
        third = a_request(learner=THIRD, at=NOW + timedelta(seconds=1), suffix="c")
        for request in (first, second, third):
            queue.add(request)

        assert queue.position(first.id) == 0
        assert queue.position(second.id) == 1
        assert queue.position(third.id) == 2

        queue.reserve(NOW + timedelta(seconds=2), worker="r1")
        # 走り始めた要求には順番が無い。後ろは 1 つずつ進む。
        assert queue.position(first.id) is None
        assert queue.position(second.id) == 0
        assert queue.position(RunRequestId("run_" + "f" * 32)) is None


def test_the_latest_request_is_the_newest_in_any_state(database: Database) -> None:
    for queue in _both(database):
        assert queue.latest_for(LEARNER) is None
        first = a_request(at=NOW)
        queue.add(first)
        running = queue.reserve(NOW, worker="r1")
        assert running is not None
        queue.update(running.completed(NOW, an_outcome()))
        second = a_request(at=NOW + timedelta(seconds=5))
        queue.add(second)

        latest = queue.latest_for(LEARNER)
        assert latest is not None and latest.id == second.id


def test_stale_waiting_requests_expire_and_lost_leases_fail(database: Database) -> None:
    for queue in _both(database):
        waiting = a_request(learner=LEARNER, at=NOW)
        fresh = a_request(learner=OTHER, at=NOW + timedelta(seconds=25))
        held = a_request(learner=THIRD, at=NOW)
        queue.add(held)
        queue.reserve(NOW, worker="r1", lease_seconds=20)
        queue.add(waiting)
        queue.add(fresh)

        changed = queue.expire_stale(NOW + timedelta(seconds=30), stale_after=30)

        assert changed == 2
        assert queue.get(waiting.id).state is RunState.EXPIRED
        lost = queue.get(held.id)
        assert lost.state is RunState.FAILED and lost.error == RUNNER_LOST
        assert queue.get(fresh.id).state is RunState.QUEUED
        # 片付けたので、その学習者は次を積める。
        queue.add(a_request(learner=LEARNER, at=NOW + timedelta(seconds=31)))


def test_finished_requests_are_purged(database: Database) -> None:
    """**結果は残さない**（ADR 0024 §1）。行には学習者のコードが入っている。"""
    for queue in _both(database):
        old = a_request(learner=LEARNER, at=NOW)
        queue.add(old)
        running = queue.reserve(NOW, worker="r1")
        assert running is not None
        queue.update(running.completed(NOW + timedelta(seconds=1), an_outcome()))
        waiting = a_request(learner=OTHER, at=NOW)
        queue.add(waiting)

        purged = queue.purge_finished(NOW + timedelta(minutes=10))

        assert purged == 1
        assert queue.get(old.id) is None
        # まだ終わっていない要求は、どれだけ古くても消さない。
        assert queue.get(waiting.id) is not None


def test_run_requests_never_touch_the_grading_queue(database: Database) -> None:
    """**採点キューとは混ざらない**（ADR 0024 §1・不変条件 I3）。

    実行要求を積んでも採点ジョブは増えず、採点ジョブが待っていても runner の
    取得には現れない ── 逆も同じ。どちらかが止まっても、もう片方は進む。
    """
    submission = SubmissionId(new_id("sub"))
    with database.unit_of_work() as uow:
        uow.run_requests.add(a_request())
        uow.jobs.enqueue(
            GradingJob(
                id=new_id("job"),
                tenant_id=TENANT,
                submission_id=submission,
                task_version_id=VERSION,
                subject_profile="cs_lang_c_intro",
                idempotency_key=job_idempotency_key(submission, JobReason.SUBMISSION),
                available_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        uow.commit()

    with database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 1
        job = uow.jobs.reserve(NOW, worker="w1", lease_seconds=60)
        assert job is not None and job.submission_id == submission
        assert uow.jobs.reserve(NOW, worker="w2", lease_seconds=60) is None

        run = uow.run_requests.reserve(NOW, worker="r1")
        assert run is not None and run.learner_id == LEARNER
        assert uow.run_requests.reserve(NOW, worker="r2") is None
