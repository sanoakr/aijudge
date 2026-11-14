"""戻したジョブを別のワーカーが取ったら、最初のワーカーはもう持っていない（#399・#686）。

`held_by` は `worker` と `attempts` の両方を見る。`attempts` だけでは区別できない
経路が実在する: w1 が取る（attempts=1）→ `released` / `moved_to` で試行に数えず
戻す（attempts=0）→ w2 が取る（attempts=1）。このとき w1 が予約時に受け取った値と
attempts が同じになる。worker を比べないと、w1 に残っていた採点が完了を書き、
同じ提出が二重に採点される。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import GradingJobId, SubmissionId, TaskVersionId, TenantId
from aijudge_submission import GradingJob, JobLane, JobReason, job_idempotency_key

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "1" * 32)


def a_job() -> GradingJob:
    return GradingJob(
        id=GradingJobId("job_" + "3" * 32),
        tenant_id=TenantId("ten_" + "0" * 32),
        submission_id=SUBMISSION,
        task_version_id=TaskVersionId("tsv_" + "4" * 32),
        subject_profile="cs_lang_c_intro",
        idempotency_key=job_idempotency_key(SUBMISSION, JobReason.SUBMISSION),
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    "give_back",
    [
        lambda job, at: job.released(at),
        lambda job, at: job.moved_to(JobLane.TRANSCRIPTION, at),
    ],
    ids=["released", "moved_to"],
)
def test_the_first_worker_loses_the_job_even_with_the_same_attempt_count(give_back) -> None:
    first = a_job().reserved(NOW, worker="w1", lease_seconds=60.0)
    later = NOW + timedelta(seconds=10)
    retaken = give_back(first, later).reserved(later, worker="w2", lease_seconds=60.0)

    # attempts だけでは区別できない状況であることを先に確かめる。
    assert retaken.attempts == first.attempts == 1
    assert not retaken.held_by(first)
    assert retaken.held_by(retaken)
