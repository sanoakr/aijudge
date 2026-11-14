"""採点キューの絞り込みと寝かせの規則を、インメモリと SQL の両方に当てる（#686）。

`memory.py` 冒頭は「規則は本番と同じにしてある」と言うが、キューの絞り込み
（列・段階）、`position_in_queue` の列、`reschedule_waiting`・`release_waiting`・
`failed_for` について、それを保証するテストがインメモリ側に無かった。
インメモリの `reserve` が列や段階を無視すると、それで組んだテストや開発環境では
決定的段階のワーカーが AI 段階や書き起こしの列のジョブを取る。SQL の規則を
変えたときにインメモリを直し忘れても気づけない。

**同じテストを両方に当てる**（`test_repositories.py` の `job_queue` と同じ形）。

`release_waiting` の SQL 実装が `document` を更新しない件は #680 で扱う。
ここでは件数と列（`waiting_count`・`reserve`）だけを見て、文書の
`available_at` には触れない。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import GradingPhase
from aijudge_core.ids import GradingJobId, GradingRunId, SubmissionId, TaskVersionId, TenantId
from aijudge_persistence import Database
from aijudge_submission import (
    GradingJob,
    InMemoryJobQueue,
    JobLane,
    JobReason,
    JobState,
)

TENANT = TenantId("ten_" + "0" * 32)
TASK_VERSION = TaskVersionId("tsv_" + "3" * 32)
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)
BASE_RUN = GradingRunId("grn_" + "b" * 32)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "sqlite"] + (["postgres"] if POSTGRES_URL else []))
def queue(request) -> Iterator[object]:
    """インメモリのキューか、SQL の UnitOfWork のキューを返す。"""
    if request.param == "memory":
        yield InMemoryJobQueue()
        return
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    with db.unit_of_work() as uow:
        yield uow.jobs
        uow.commit()
    db.dispose()


def _job(n: int, **fields) -> GradingJob:
    """n 番目のジョブ。`created_at` は n 秒ずらす（小さいほど古く、先に取られる）。"""
    defaults = {
        "id": GradingJobId(f"gjb_{n:032d}"),
        "tenant_id": TENANT,
        "submission_id": SubmissionId(f"sub_{n:032d}"),
        "task_version_id": TASK_VERSION,
        "subject_profile": "cs_lang_c_intro",
        "phase": GradingPhase.DETERMINISTIC,
        "idempotency_key": f"key-{n}",
        "available_at": NOW,
        "created_at": NOW + timedelta(seconds=n),
        "updated_at": NOW,
    }
    return GradingJob(**(defaults | fields))


def _sub(n: int) -> SubmissionId:
    return SubmissionId(f"sub_{n:032d}")


# --------------------------------------------------------------------------
# 取るときの絞り込み
# --------------------------------------------------------------------------


def test_a_worker_on_one_lane_skips_an_older_job_on_another(queue) -> None:
    """書き起こしの列のジョブは、先に積まれていてもふつうの列のワーカーに渡らない。"""
    queue.enqueue(_job(1, lane=JobLane.TRANSCRIPTION))
    queue.enqueue(_job(2))

    taken = queue.reserve(NOW, worker="det", lease_seconds=60.0, lane=JobLane.DEFAULT)
    assert taken is not None
    assert taken.submission_id == _sub(2)
    assert queue.reserve(NOW, worker="det", lease_seconds=60.0, lane=JobLane.DEFAULT) is None

    other = queue.reserve(NOW, worker="tr", lease_seconds=60.0, lane=JobLane.TRANSCRIPTION)
    assert other is not None
    assert other.submission_id == _sub(1)


def test_a_worker_on_one_phase_skips_an_older_job_of_another(queue) -> None:
    """決定的段階のワーカーは、先に積まれた AI 段階のジョブを取らない。"""
    queue.enqueue(_job(1, phase=GradingPhase.AI, base_run_id=BASE_RUN))
    queue.enqueue(_job(2))

    taken = queue.reserve(NOW, worker="det", lease_seconds=60.0, phase=GradingPhase.DETERMINISTIC)
    assert taken is not None
    assert taken.submission_id == _sub(2)
    assert (
        queue.reserve(NOW, worker="det", lease_seconds=60.0, phase=GradingPhase.DETERMINISTIC)
        is None
    )

    ai = queue.reserve(NOW, worker="ai", lease_seconds=60.0, phase=GradingPhase.AI)
    assert ai is not None
    assert ai.submission_id == _sub(1)


def test_the_position_counts_only_the_same_lane(queue) -> None:
    """別の列の待ちや処理中は、自分の前に数えない（その列のワーカーは別にいる）。"""
    queue.enqueue(_job(1, lane=JobLane.TRANSCRIPTION))
    queue.enqueue(
        _job(
            2,
            lane=JobLane.TRANSCRIPTION,
            state=JobState.RUNNING,
            attempts=1,
            worker="tr",
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    )
    queue.enqueue(_job(3))
    queue.enqueue(_job(4))

    assert queue.position_in_queue(_sub(3), GradingPhase.DETERMINISTIC, NOW) == 0
    assert queue.position_in_queue(_sub(4), GradingPhase.DETERMINISTIC, NOW) == 1
    # 書き起こしの列の中では、処理中の 1 件が前にいる。
    assert queue.position_in_queue(_sub(1), GradingPhase.DETERMINISTIC, NOW) == 1


# --------------------------------------------------------------------------
# 採点開始の付け替え（reschedule_waiting）
# --------------------------------------------------------------------------


def test_rescheduling_moves_only_a_first_attempt_that_is_still_waiting(queue) -> None:
    """**提出の採点待ちだけを動かす。** 再試行の待ちと、いま取れるジョブは触らない。"""
    queue.enqueue(_job(1, available_at=LATER))  # 動かす: 採点開始待ち
    queue.enqueue(_job(2, available_at=LATER, attempts=1))  # 再試行の待ち（間隔の制御）
    queue.enqueue(_job(3))  # いま取れる（寝かせ直さない）
    queue.enqueue(_job(4, available_at=LATER, reason=JobReason.REGRADE))  # 再採点

    new_start = NOW + timedelta(hours=1)
    moved = queue.reschedule_waiting([_sub(n) for n in (1, 2, 3, 4)], new_start, NOW)

    assert moved == 1
    assert queue.get(_job(1).id).available_at == new_start
    assert queue.get(_job(2).id).available_at == LATER
    assert queue.get(_job(3).id).available_at == NOW
    assert queue.get(_job(4).id).available_at == LATER


# --------------------------------------------------------------------------
# 一括採点（release_waiting）・待ちの件数・失敗
# --------------------------------------------------------------------------


def test_releasing_wakes_only_the_waiting_jobs_of_the_given_submissions(queue) -> None:
    queue.enqueue(_job(1, available_at=LATER))  # 起こす
    queue.enqueue(_job(2))  # すでに取れる（触らない）
    queue.enqueue(_job(3, available_at=LATER))  # 対象外の提出
    assert queue.waiting_count([_sub(1), _sub(2), _sub(3)], NOW) == 2

    released = queue.release_waiting([_sub(1), _sub(2)], NOW)

    assert released == 1
    assert queue.waiting_count([_sub(1), _sub(2)], NOW) == 0
    assert queue.waiting_count([_sub(3)], NOW) == 1
    taken = {
        job.submission_id
        for job in (queue.reserve(NOW, worker="w", lease_seconds=60.0) for _ in range(3))
        if job is not None
    }
    assert taken == {_sub(1), _sub(2)}


def test_failed_for_lists_only_the_failed_jobs_of_the_given_submissions(queue) -> None:
    failed = _job(1).reserved(NOW, worker="w").failed(NOW, "boom", permanent=True)
    queue.enqueue(failed)
    queue.enqueue(_job(2))
    queue.enqueue(_job(3).reserved(NOW, worker="w").failed(NOW, "other", permanent=True))

    found = queue.failed_for([_sub(1), _sub(2)])

    assert [job.id for job in found] == [failed.id]
    assert found[0].state is JobState.FAILED


def test_awaiting_is_per_phase_and_ignores_finished_jobs(queue) -> None:
    queue.enqueue(_job(1))
    queue.enqueue(_job(2, state=JobState.DONE, grading_run_id=GradingRunId("grn_" + "c" * 32)))

    assert queue.awaiting(_sub(1), GradingPhase.DETERMINISTIC)
    assert not queue.awaiting(_sub(1), GradingPhase.AI)
    assert not queue.awaiting(_sub(2), GradingPhase.DETERMINISTIC)
