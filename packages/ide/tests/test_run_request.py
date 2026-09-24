"""試しの実行の要求と受付の規則（ADR 0024・設計書 §8.1）。

キューの実装どうしの一致は `packages/persistence/tests/test_run_repository.py`
が見る。ここで見るのは、実装に依らない規則 ── 状態遷移と受付の検査である。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_ide import (
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    RUNNER_LOST,
    InMemoryRunQueue,
    RefusalReason,
    RunOutcome,
    RunPolicy,
    RunRefused,
    RunRequest,
    RunRequestId,
    RunStage,
    RunState,
    clip_for_display,
    request_run,
    view_run,
)

TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "1" * 32)
OTHER = UserId("usr_" + "2" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
SOURCE = '#include <stdio.h>\nint main(void){puts("hi");}\n'


def ask(queue: InMemoryRunQueue, *, at: datetime = NOW, learner: UserId = LEARNER, **kw):
    return request_run(
        queue,
        tenant_id=TENANT,
        learner_id=learner,
        task_version_id=VERSION,
        source=kw.pop("source", SOURCE),
        now=at,
        **kw,
    )


def an_outcome() -> RunOutcome:
    return RunOutcome(stage=RunStage.RUN, exit_code=0, stdout="hi\n")


# -- 状態遷移 ----------------------------------------------------------------


def test_a_request_moves_queued_running_done() -> None:
    request = ask(InMemoryRunQueue())
    assert request.state is RunState.QUEUED

    running = request.reserved(NOW, worker="r1", lease_seconds=60)
    assert running.state is RunState.RUNNING
    assert running.lease_expires_at == NOW + timedelta(seconds=60)
    assert running.started_at == NOW

    done = running.completed(NOW + timedelta(seconds=1), an_outcome())
    assert done.state is RunState.DONE
    assert done.lease_expires_at is None and done.worker is None
    assert done.outcome is not None and done.outcome.stdout == "hi\n"


def test_only_a_waiting_request_can_expire() -> None:
    """**走っている要求は期限切れにしない。** 古くなるのは待っている間だけで、
    走り始めた要求を捨てると、動かした結果を学習者が受け取れない。
    """
    request = ask(InMemoryRunQueue())
    running = request.reserved(NOW, worker="r1")
    with pytest.raises(ValueError):
        running.expired(NOW)
    assert not running.is_stale(NOW + timedelta(hours=1))


def test_a_finished_request_cannot_be_reopened() -> None:
    done = ask(InMemoryRunQueue()).reserved(NOW, worker="r1").completed(NOW, an_outcome())
    with pytest.raises(ValueError):
        done.reserved(NOW, worker="r2")
    with pytest.raises(ValueError):
        done.failed(NOW, "late")


def test_a_request_cannot_carry_both_a_sample_and_free_stdin() -> None:
    """**サンプルの中身は学習者から受け取らない。** 名前だけ受け取り、runner が
    課題から引く ── 両方を持てると、どちらを信じるかが曖昧になる。
    """
    with pytest.raises(ValueError):
        RunRequest(
            id=RunRequestId("run_" + "a" * 32),
            tenant_id=TENANT,
            learner_id=LEARNER,
            task_version_id=VERSION,
            source=SOURCE,
            stdin="1 2\n",
            sample_name="sample1",
            created_at=NOW,
            updated_at=NOW,
        )


def test_output_is_clipped_for_the_screen() -> None:
    text, cut = clip_for_display("x" * 10, limit=4)
    assert (text, cut) == ("xxxx", True)
    assert clip_for_display("abc", limit=4) == ("abc", False)


# -- 受付 --------------------------------------------------------------------


def test_one_learner_waits_on_one_request_at_a_time() -> None:
    queue = InMemoryRunQueue()
    ask(queue)
    with pytest.raises(RunRefused) as refused:
        ask(queue, at=NOW + timedelta(seconds=10))
    assert refused.value.reason is RefusalReason.ALREADY_PENDING


def test_other_learners_are_not_held_up() -> None:
    queue = InMemoryRunQueue()
    ask(queue)
    assert ask(queue, learner=OTHER).state is RunState.QUEUED


def test_runs_must_be_spaced_by_the_cooldown() -> None:
    queue = InMemoryRunQueue()
    first = ask(queue)
    queue.update(first.reserved(NOW, worker="r").completed(NOW, an_outcome()))

    with pytest.raises(RunRefused) as refused:
        ask(queue, at=NOW + timedelta(seconds=1))
    assert refused.value.reason is RefusalReason.TOO_SOON
    assert refused.value.retry_after == pytest.approx(2.0)

    assert ask(queue, at=NOW + timedelta(seconds=3)).state is RunState.QUEUED


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"source": "   \n"}, RefusalReason.EMPTY_SOURCE),
        ({"source": "a" * (MAX_SOURCE_BYTES + 1)}, RefusalReason.SOURCE_TOO_LARGE),
        ({"stdin": "a" * (MAX_STDIN_BYTES + 1)}, RefusalReason.STDIN_TOO_LARGE),
        ({"stdin": "1\n", "sample_name": "s1"}, RefusalReason.BOTH_INPUTS),
    ],
)
def test_malformed_requests_are_refused(kwargs: dict, reason: RefusalReason) -> None:
    queue = InMemoryRunQueue()
    with pytest.raises(RunRefused) as refused:
        ask(queue, **kwargs)
    assert refused.value.reason is reason
    assert queue.latest_for(LEARNER) is None


def test_the_size_limit_counts_bytes_not_characters() -> None:
    """日本語のコメントは 1 文字 3 バイト。sandbox に書くのはバイト列である。"""
    queue = InMemoryRunQueue()
    chars = MAX_SOURCE_BYTES // 3 + 1
    with pytest.raises(RunRefused) as refused:
        ask(queue, source="あ" * chars)
    assert refused.value.reason is RefusalReason.SOURCE_TOO_LARGE


def test_a_stuck_request_expires_so_the_learner_can_run_again() -> None:
    """**runner が全部止まっていても、30 秒で次を積める**（ADR 0024 の帰結）。

    片付けを runner にだけ任せると、runner が止まっている間、学習者は
    「前の実行が終わるまでお待ちください」から永久に抜けられない。
    """
    queue = InMemoryRunQueue()
    first = ask(queue)
    later = NOW + timedelta(seconds=31)

    second = ask(queue, at=later)

    assert queue.get(first.id).state is RunState.EXPIRED
    assert second.state is RunState.QUEUED


def test_a_lost_runner_fails_its_request() -> None:
    queue = InMemoryRunQueue()
    first = ask(queue)
    queue.reserve(NOW, worker="r1", lease_seconds=60)

    view = view_run(queue, first.id, learner_id=LEARNER, now=NOW + timedelta(seconds=61))

    assert view is not None
    assert view.request.state is RunState.FAILED
    assert view.request.error == RUNNER_LOST


def test_polling_reports_the_queue_position() -> None:
    queue = InMemoryRunQueue()
    ask(queue, learner=OTHER)
    mine = ask(queue, at=NOW + timedelta(seconds=1))

    view = view_run(queue, mine.id, learner_id=LEARNER, now=NOW + timedelta(seconds=2))

    assert view is not None and view.ahead == 1


def test_someone_elses_request_does_not_exist() -> None:
    """**他人の要求は「無い」と答える。** 「ある」と言うだけで、ID を推測した
    誰かに他人が実行中であることが漏れる。
    """
    queue = InMemoryRunQueue()
    theirs = ask(queue, learner=OTHER)
    assert view_run(queue, theirs.id, learner_id=LEARNER, now=NOW) is None


def test_the_policy_can_be_tightened() -> None:
    queue = InMemoryRunQueue()
    with pytest.raises(RunRefused):
        ask(queue, source="a" * 11, policy=RunPolicy(max_source_bytes=10))
