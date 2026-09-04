"""ジョブの生存性を固定する（S3）。

締切前のバーストで採点が詰まったとき、どの提出も「いつか必ず結果が返る」
状態を保てるかどうかがここで決まる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import (
    GradingJobId,
    GradingRunId,
    SubmissionId,
    TaskVersionId,
    TenantId,
)
from aijudge_submission import GradingJob, JobReason, JobState, job_idempotency_key
from aijudge_submission.memory import InMemoryJobQueue

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "1" * 32)
RUN = GradingRunId("grn_" + "2" * 32)


def make(**overrides) -> GradingJob:
    base = {
        "id": GradingJobId("job_" + "3" * 32),
        "tenant_id": TenantId("ten_" + "0" * 32),
        "submission_id": SUBMISSION,
        "task_version_id": TaskVersionId("tsv_" + "4" * 32),
        "subject_profile": "cs_intro_c",
        "idempotency_key": job_idempotency_key(SUBMISSION, JobReason.SUBMISSION),
        "available_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return GradingJob.model_validate(base)


# --------------------------------------------------------------------------
# 試行回数とリトライ
# --------------------------------------------------------------------------


def test_the_attempt_is_counted_when_the_job_is_taken_not_when_it_succeeds() -> None:
    """採点中にワーカーが落ちても同じジョブを無限に取り直さない。

    成功時に数えていると、プロセスを殺すような提出で無限ループになる。
    """
    job = make().reserved(NOW, worker="w1")
    assert job.attempts == 1
    assert job.state is JobState.RUNNING


def test_a_transient_failure_goes_back_to_the_queue_with_a_delay() -> None:
    job = make().reserved(NOW, worker="w1").failed(NOW, "LLM timeout", backoff_seconds=30.0)
    assert job.state is JobState.QUEUED
    assert job.available_at == NOW + timedelta(seconds=30)
    assert job.last_error == "LLM timeout"
    assert not job.is_available(NOW), "バックオフ中に取れてしまう"
    assert job.is_available(NOW + timedelta(seconds=30))


def test_the_backoff_grows() -> None:
    """締切前のバーストで再試行が固まらないように。"""
    job = make()
    delays = []
    for _ in range(2):
        job = job.reserved(job.available_at, worker="w1")
        before = job.available_at
        job = job.failed(before, "boom", backoff_seconds=30.0)
        delays.append((job.available_at - before).total_seconds())
    assert delays == [30.0, 60.0]


def test_retries_stop_at_the_limit() -> None:
    """無限に GPU を回さない。上限に達したら人間が見る。"""
    job = make(max_attempts=2)
    for _ in range(2):
        job = job.reserved(NOW, worker="w1").failed(NOW, "boom")
    assert job.state is JobState.FAILED
    assert job.attempts == 2
    assert job.terminal


def test_a_permanent_failure_stops_immediately() -> None:
    """課題定義が壊れている類の失敗は、何度やっても同じ。"""
    job = make(max_attempts=5).reserved(NOW, worker="w1")
    job = job.failed(NOW, "profile names an evaluator that does not exist", permanent=True)
    assert job.state is JobState.FAILED
    assert job.attempts == 1, "恒久的な失敗でリトライしている"


def test_a_failed_job_must_say_why() -> None:
    with pytest.raises(ValueError, match="must carry the error"):
        make(state=JobState.FAILED, attempts=3)


def test_a_completed_job_must_reference_its_run() -> None:
    """どの採点結果になったのか辿れないジョブは残さない。"""
    with pytest.raises(ValueError, match="must reference its GradingRun"):
        make(state=JobState.DONE)


def test_a_terminal_job_cannot_be_reserved_again() -> None:
    job = make().reserved(NOW, worker="w1").completed(NOW, RUN)
    with pytest.raises(ValueError, match="cannot reserve"):
        job.reserved(NOW, worker="w2")


# --------------------------------------------------------------------------
# リース — 死んだワーカーのジョブを拾う
# --------------------------------------------------------------------------


def test_a_running_job_holds_a_lease() -> None:
    job = make().reserved(NOW, worker="w1", lease_seconds=600.0)
    assert job.lease_expires_at == NOW + timedelta(seconds=600)


def test_a_running_job_without_a_lease_is_refused() -> None:
    """リース無しの RUNNING は、誰も拾えないまま永久に残る。"""
    with pytest.raises(ValueError, match="must hold a lease"):
        make(state=JobState.RUNNING, attempts=1)


def test_an_expired_lease_makes_the_job_available_again() -> None:
    """ワーカーが死んだら別のワーカーが引き取る。

    拾わないと、その学習者だけ結果が返らないまま放置される。
    """
    job = make().reserved(NOW, worker="w1", lease_seconds=60.0)
    assert not job.is_available(NOW)
    assert job.is_available(NOW + timedelta(seconds=60))


def test_the_queue_hands_an_expired_job_to_another_worker() -> None:
    queue = InMemoryJobQueue()
    queue.enqueue(make())

    first = queue.reserve(NOW, worker="w1", lease_seconds=60.0)
    assert first is not None
    assert queue.reserve(NOW, worker="w2", lease_seconds=60.0) is None, "二重に配られた"

    later = NOW + timedelta(seconds=120)
    second = queue.reserve(later, worker="w2", lease_seconds=60.0)
    assert second is not None
    assert second.id == first.id
    assert second.attempts == 2
    assert second.worker == "w2"


# --------------------------------------------------------------------------
# キュー
# --------------------------------------------------------------------------


def test_enqueueing_the_same_key_returns_the_existing_job() -> None:
    """二重投入で GPU を二度回さない。"""
    queue = InMemoryJobQueue()
    first = queue.enqueue(make())
    second = queue.enqueue(make(id=GradingJobId("job_" + "9" * 32)))
    assert second.id == first.id


def test_jobs_are_handed_out_oldest_first() -> None:
    """先に出した学習者が先に返る。締切前でも順序を守る。"""
    queue = InMemoryJobQueue()
    later = make(
        id=GradingJobId("job_" + "a" * 32),
        idempotency_key="later",
        available_at=NOW + timedelta(seconds=10),
        created_at=NOW + timedelta(seconds=10),
    )
    earlier = make(id=GradingJobId("job_" + "b" * 32), idempotency_key="earlier")
    queue.enqueue(later)
    queue.enqueue(earlier)

    taken = queue.reserve(NOW + timedelta(seconds=30), worker="w1", lease_seconds=60.0)
    assert taken is not None
    assert taken.id == earlier.id


def test_a_worker_can_take_only_its_own_subject() -> None:
    """GPU を食う科目と食わない科目のキューを分けられるように。"""
    queue = InMemoryJobQueue()
    queue.enqueue(make(subject_profile="math_calculus", idempotency_key="math"))
    assert queue.reserve(NOW, worker="w1", lease_seconds=60.0, subject_profile="cs_intro_c") is None
    assert (
        queue.reserve(NOW, worker="w1", lease_seconds=60.0, subject_profile="math_calculus")
        is not None
    )


def test_pending_count_covers_queued_and_running() -> None:
    """締切前の滞留を見る指標。実行中も「まだ返っていない」に数える。"""
    queue = InMemoryJobQueue()
    queue.enqueue(make())
    assert queue.pending_count() == 1
    taken = queue.reserve(NOW, worker="w1", lease_seconds=60.0)
    assert taken is not None
    assert queue.pending_count() == 1
    queue.update(taken.completed(NOW, RUN))
    assert queue.pending_count() == 0


def _q3():
    """3 提出ぶんのジョブ。作成順に s1 → s2 → s3。すべて今すぐ取れる。"""
    queue = InMemoryJobQueue()
    subs = [SubmissionId("sub_" + c * 32) for c in ("a", "b", "c")]
    for i, sub in enumerate(subs):
        queue.enqueue(
            make(
                id=GradingJobId("job_" + str(i) * 32),
                submission_id=sub,
                idempotency_key=f"k{i}",
                created_at=NOW + timedelta(seconds=i),
            )
        )
    return queue, subs


def test_position_in_queue_counts_jobs_ahead() -> None:
    from aijudge_core import GradingPhase

    queue, subs = _q3()
    assert queue.position_in_queue(subs[0], GradingPhase.DETERMINISTIC, NOW) == 0
    assert queue.position_in_queue(subs[1], GradingPhase.DETERMINISTIC, NOW) == 1
    assert queue.position_in_queue(subs[2], GradingPhase.DETERMINISTIC, NOW) == 2


def test_position_counts_a_running_job_ahead() -> None:
    from aijudge_core import GradingPhase

    queue, subs = _q3()
    taken = queue.reserve(NOW, worker="w1", lease_seconds=60.0)
    assert taken is not None and taken.submission_id == subs[0]
    # s1 は処理中（0）、s2 の前には RUNNING が 1 件、s3 の前には RUNNING + s2。
    assert queue.position_in_queue(subs[0], GradingPhase.DETERMINISTIC, NOW) == 0
    assert queue.position_in_queue(subs[1], GradingPhase.DETERMINISTIC, NOW) == 1
    assert queue.position_in_queue(subs[2], GradingPhase.DETERMINISTIC, NOW) == 2


def test_position_is_none_when_nothing_is_queued_or_held() -> None:
    from aijudge_core import GradingPhase

    queue, subs = _q3()
    # 存在しない提出。
    unknown = SubmissionId("sub_" + "9" * 32)
    assert queue.position_in_queue(unknown, GradingPhase.DETERMINISTIC, NOW) is None
    # 別段階のジョブは無い。
    assert queue.position_in_queue(subs[0], GradingPhase.AI, NOW) is None
    # 採点開始時刻まで寝かせてあるジョブは順位を約束しない。
    held = InMemoryJobQueue()
    held_sub = SubmissionId("sub_" + "d" * 32)
    held.enqueue(
        make(
            id=GradingJobId("job_" + "d" * 32),
            submission_id=held_sub,
            idempotency_key="held",
            available_at=NOW + timedelta(minutes=30),
        )
    )
    assert held.position_in_queue(held_sub, GradingPhase.DETERMINISTIC, NOW) is None
