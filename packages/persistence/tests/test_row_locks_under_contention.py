"""行ロックが競合の下で効いていること（#399・#687）。**PostgreSQL でだけ走る。**

SQLite は行ロックを持たないので、`with_for_update` を消しても SQLite の
テストは緑のままである。ここに置くのは、ロックを外すと PostgreSQL で結果が
変わるテストだけで、CI では `check` ジョブの PostgreSQL
（`AIJUDGE_TEST_DATABASE_URL`）に対して走る。

採点キューの `reserve` の並行テストは `test_repositories.py` の
`test_concurrent_workers_never_take_the_same_job` にある。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Routing,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingJobId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_ide import RunRequest, RunRequestId, RunState
from aijudge_persistence import Base, Database
from aijudge_submission import GradingJob, ImmutabilityViolation

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None, reason="needs PostgreSQL (SQLite has no row locks)"
)

TENANT = TenantId("ten_" + "0" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "3" * 32)
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)

# 別スレッドが行ロックで待たされているかを見るまでの時間。ロックが無ければ
# ミリ秒で終わる処理なので、これだけ待って終わらなければ「待たされている」。
BLOCKED_AFTER_SECONDS = 3.0
# ロックを持ったまま相手を待つ時間。相手が同じ行を読みに来るのに十分な長さ。
HOLD_SECONDS = 0.5


@pytest.fixture
def database() -> Iterator[Database]:
    """毎テストでスキーマを作り直す（前のテストの行が見えると原因が読めない）。"""
    assert POSTGRES_URL is not None
    db = Database.connect(POSTGRES_URL, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def a_run(run_id: str, submission_id: SubmissionId) -> GradingRun:
    return GradingRun(
        id=GradingRunId(run_id),
        submission_id=submission_id,
        context=GradingContext(
            task_version_id=TASK_VERSION,
            subject_profile="cs_lang_c_intro",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "5" * 32),
                criterion_id=CriterionId("crt_" + "6" * 32),
                evaluator_result_id=EvaluatorResultId("evr_" + "7" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=3,
                score_ratio=1.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="all tests pass",
            ),
        ),
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        created_at=NOW,
    )


# --------------------------------------------------------------------------
# 採点の置き換え（`supersede`）
# --------------------------------------------------------------------------


def test_two_regrades_of_one_run_supersede_it_only_once(database: Database) -> None:
    """同じ採点を 2 つのワーカーが同時に置き換えると、ちょうど片方だけが止まる。

    同じ提出の AI 段階と再採点が同時に保存される場面（#399）。ロックが無いと
    両方が「まだ置き換わっていない」と読み、両方が書き、後から commit した側の
    `superseded_by` だけが残る ── 先の側の置き換えは黙って失われる。

    ロックがあれば後の側は先の側の commit まで待ち、`already superseded` で
    止まる（ジョブは再試行され、次は正しい最新の上に積む）。先の側は
    ロックを持ったまま `HOLD_SECONDS` 待ち、後の側が同じ行を読みに来る
    時間を作る。
    """
    submission_id = SubmissionId("sub_" + "b" * 32)
    old_id = GradingRunId("grn_" + "a" * 32)
    with database.unit_of_work() as uow:
        uow.runs.save(a_run(old_id, submission_id))
        uow.commit()

    new_ids = (GradingRunId("grn_" + "c" * 32), GradingRunId("grn_" + "d" * 32))
    outcomes: dict[GradingRunId, str] = {}
    barrier = threading.Barrier(len(new_ids))

    def regrade(new_id: GradingRunId) -> None:
        with database.unit_of_work() as uow:
            uow.runs.save(a_run(new_id, submission_id))
            barrier.wait()
            try:
                uow.runs.supersede(old_id, new_id)
            except ImmutabilityViolation:
                outcomes[new_id] = "refused"
                return
            time.sleep(HOLD_SECONDS)
            uow.commit()
            outcomes[new_id] = "won"

    threads = [threading.Thread(target=regrade, args=(new_id,)) for new_id in new_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes.values()) == ["refused", "won"], outcomes
    (winner,) = [new_id for new_id, outcome in outcomes.items() if outcome == "won"]
    with database.unit_of_work() as uow:
        old = uow.runs.get(old_id)
    assert old is not None
    assert old.superseded_by == winner


# --------------------------------------------------------------------------
# 採点ジョブの `lock`
# --------------------------------------------------------------------------


def a_job() -> GradingJob:
    return GradingJob(
        id=GradingJobId("gjb_" + "1" * 32),
        tenant_id=TENANT,
        submission_id=SubmissionId("sub_" + "1" * 32),
        task_version_id=TASK_VERSION,
        subject_profile="cs_lang_c_intro",
        idempotency_key="key-1",
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_locked_job_cannot_be_taken_over_until_its_holder_commits(database: Database) -> None:
    """`lock` は行ロックを取り、持ち主の確認と書き込みの間に横取りさせない。

    ワーカーは結果と DONE を書く前に `lock` で持ち主を確かめる（#399）。
    ロックが無いと、確かめた直後にリースの切れたジョブを別のワーカーが
    取り直し、2 つのワーカーが同じ提出を採点したことになる。

    取り直しの `reserve` は `skip_locked` なので、ロックがあれば待たずに
    「取れるものは無い」と答える。ロックを放した後は取れる。
    """
    job = a_job()
    with database.unit_of_work() as uow:
        uow.jobs.enqueue(job)
        assert uow.jobs.reserve(NOW, worker="w1", lease_seconds=60.0) is not None
        uow.commit()

    lease_expired = NOW + timedelta(seconds=120)
    with database.unit_of_work() as holder:
        locked = holder.jobs.lock(job.id)
        assert locked is not None and locked.worker == "w1"
        with database.unit_of_work() as rival:
            assert rival.jobs.reserve(lease_expired, worker="w2", lease_seconds=60.0) is None
            rival.commit()
        holder.commit()

    with database.unit_of_work() as rival:
        taken = rival.jobs.reserve(lease_expired, worker="w2", lease_seconds=60.0)
        rival.commit()
    assert taken is not None and taken.worker == "w2"


# --------------------------------------------------------------------------
# 試しの実行キュー（ADR 0024）
# --------------------------------------------------------------------------


def a_request(index: int, at: datetime = NOW) -> RunRequest:
    return RunRequest(
        id=RunRequestId(f"run_{index:032d}"),
        tenant_id=TENANT,
        learner_id=UserId(f"usr_{index:032d}"),
        task_version_id=TASK_VERSION,
        source="print(1)\n",
        stdin="",
        created_at=at,
        updated_at=at,
    )


def test_concurrent_runners_never_take_the_same_request(database: Database) -> None:
    """runner を何本立てても、1 つの要求を実行するのは 1 本だけ。

    `reserve` に `FOR UPDATE SKIP LOCKED` が無いと、2 本が同じ QUEUED の要求を
    読んで両方が実行し、後から書いた側の結果で上書きされる（学習者のコードが
    コンテナで二度走る）。本番では runner が 4 本立っている（ADR 0024）。

    取ってから commit するまでに少し間を置き、読みと書きの間に他の runner が
    入り込める時間を作る（実際の runner も取ってから状態を書くまでに間がある）。
    """
    request_count = 24
    with database.unit_of_work() as uow:
        for index in range(request_count):
            uow.run_requests.add(a_request(index))
        uow.commit()

    taken: list[str] = []
    lock = threading.Lock()
    runner_count = 4
    barrier = threading.Barrier(runner_count)

    def drain(name: str) -> None:
        barrier.wait()
        while True:
            with database.unit_of_work() as uow:
                request = uow.run_requests.reserve(NOW, worker=name, lease_seconds=600.0)
                if request is None:
                    uow.commit()
                    return
                time.sleep(0.01)
                uow.commit()
            with lock:
                taken.append(str(request.id))

    threads = [threading.Thread(target=drain, args=(f"r{n}",)) for n in range(runner_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(taken)) == len(taken), "同じ要求が二度配られた"
    assert len(taken) == request_count, f"取り漏らし: {len(taken)}/{request_count}"


def test_expiring_stale_requests_skips_the_one_a_runner_is_taking(database: Database) -> None:
    """片付けは、runner がいま取っている要求を待たずに飛ばす。

    `expire_stale` に `FOR UPDATE SKIP LOCKED` が無いと、runner が取って
    commit する前の要求を「待ちすぎ」と読み、runner の commit を待ったうえで
    RUNNING を EXPIRED で上書きする。受付のリクエストはその間止まり、
    runner は実行中の要求を失う。

    ここでは runner の側のトランザクションを開いたまま、別スレッドで片付けを
    走らせる。ロックを飛ばせば即座に 0 件で終わる。
    """
    with database.unit_of_work() as uow:
        uow.run_requests.add(a_request(1))
        uow.commit()

    later = NOW + timedelta(seconds=60)
    expired: list[int] = []

    def expire() -> None:
        with database.unit_of_work() as uow:
            expired.append(uow.run_requests.expire_stale(later, stale_after=30.0))
            uow.commit()

    sweeper = threading.Thread(target=expire)
    with database.unit_of_work() as runner:
        taken = runner.run_requests.reserve(later, worker="r1", lease_seconds=600.0)
        assert taken is not None
        sweeper.start()
        sweeper.join(timeout=BLOCKED_AFTER_SECONDS)
        blocked = sweeper.is_alive()
        runner.commit()
    sweeper.join()

    assert not blocked, "片付けが runner の行ロックを待った"
    assert expired == [0]
    with database.unit_of_work() as uow:
        stored = uow.run_requests.get(taken.id)
    assert stored is not None and stored.state is RunState.RUNNING
