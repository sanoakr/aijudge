"""提出受付の規則を固定する（S3）。

固定したいのは 3 つ。

冪等   二重投入で提出もジョブも増えない。二重投入は事故ではなく日常。
順序   提出時にジョブが積まれる。レビューも採点の完了も待たない。
不変   提出後の書き換えと採点結果の上書きを拒否する（P8）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import ArtifactKind, SubmissionState
from aijudge_core.events import SubmissionCreated
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import (
    ImmutabilityViolation,
    IncomingFile,
    JobReason,
    JobState,
    SubmissionRejected,
    SubmissionService,
    content_idempotency_key,
    in_memory_backend,
)

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
PROFILE = "cs_intro_c"

START = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def backend():
    uow, store = in_memory_backend()
    clock = FrozenClock()
    service = SubmissionService(lambda: uow, store, clock=clock)
    return service, uow, store, clock


def code(text: str = "int main(void){return 0;}") -> list[IncomingFile]:
    return [IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=text.encode())]


def accept(service, files=None, **overrides):
    kwargs = {
        "tenant_id": TENANT,
        "task_version_id": TASK,
        "learner_id": LEARNER,
        "subject_profile": PROFILE,
        "files": files if files is not None else code(),
    }
    kwargs.update(overrides)
    return service.accept(**kwargs)


# --------------------------------------------------------------------------
# 受付
# --------------------------------------------------------------------------


def test_a_submission_is_accepted_and_a_job_is_queued(backend) -> None:
    """提出したら採点ジョブが積まれる。レビューを待たない（ADR 0007）。"""
    service, uow, _, _ = backend
    result = accept(service)

    assert result.submission.state is SubmissionState.SUBMITTED
    assert result.submission.submitted_at is not None
    assert result.job.state is JobState.QUEUED
    assert result.job.reason is JobReason.SUBMISSION
    assert uow.jobs.pending_count() == 1


def test_the_payload_is_stored_and_readable_back(backend) -> None:
    service, _, store, _ = backend
    result = accept(service, code("hello"))
    artifact = result.submission.artifacts[0]
    assert store.get(artifact.storage_key) == b"hello"
    assert artifact.byte_size == 5


def test_the_content_hash_is_recorded(backend) -> None:
    """根拠のスパン有効性判定に使う（core/spans.py）。"""
    service, _, _, _ = backend
    artifact = accept(service, code("x")).submission.artifacts[0]
    assert artifact.content_hash.startswith("sha256:")


def test_an_empty_submission_is_rejected(backend) -> None:
    service, _, _, _ = backend
    with pytest.raises(SubmissionRejected, match="提出物がありません"):
        accept(service, [])


def test_a_submission_created_event_goes_to_the_outbox(backend) -> None:
    """S3 → S5 はイベントだけで結合する（設計方針 §2.3）。"""
    service, uow, _, _ = backend
    result = accept(service)

    events = uow.outbox.unpublished()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, SubmissionCreated)
    assert event.submission_id == result.submission.id
    assert event.subject_profile == PROFILE


def test_everything_is_committed_once(backend) -> None:
    """提出・ジョブ・イベントは同時に成立する。片方だけ残らない。"""
    service, uow, _, _ = backend
    accept(service)
    assert uow.commits == 1


# --------------------------------------------------------------------------
# 冪等 — 二重投入は日常
# --------------------------------------------------------------------------


def test_the_same_content_submitted_twice_does_not_create_a_second_submission(backend) -> None:
    """ブラウザの二度押しで提出が増えない。"""
    service, uow, _, _ = backend
    first = accept(service)
    second = accept(service)

    assert second.deduplicated
    assert second.submission.id == first.submission.id
    assert second.job.id == first.job.id
    assert uow.jobs.pending_count() == 1
    assert len(uow.outbox.all_events()) == 1


def test_changed_content_is_a_new_attempt(backend) -> None:
    """出し直しは新しい提出。回数が増える。"""
    service, _, _, _ = backend
    first = accept(service, code("version one"))
    second = accept(service, code("version two"))

    assert not second.deduplicated
    assert second.submission.id != first.submission.id
    assert first.submission.attempt == 1
    assert second.submission.attempt == 2


def test_an_explicit_idempotency_key_wins(backend) -> None:
    """HTTP のリクエスト ID を渡す経路。中身が違っても同じ提出とみなす。"""
    service, _, _, _ = backend
    first = accept(service, code("a"), idempotency_key="req-1")
    second = accept(service, code("b"), idempotency_key="req-1")
    assert second.submission.id == first.submission.id


def test_the_content_key_is_stable_and_order_independent() -> None:
    """ファイルの並び順で鍵が変わると、同じ提出が二重に入る。"""
    a = IncomingFile(filename="a.c", kind=ArtifactKind.CODE, payload=b"1")
    b = IncomingFile(filename="b.c", kind=ArtifactKind.CODE, payload=b"2")
    assert content_idempotency_key(TASK, LEARNER, [a, b]) == content_idempotency_key(
        TASK, LEARNER, [b, a]
    )


def test_a_different_learner_gets_a_different_key() -> None:
    """同じコードを出した別の学習者を同一視しない。"""
    other = UserId("usr_" + "9" * 32)
    assert content_idempotency_key(TASK, LEARNER, code()) != content_idempotency_key(
        TASK, other, code()
    )


def test_a_missing_job_is_repaired_on_resubmission(backend) -> None:
    """提出は残ったがジョブが入らなかった状態を放置しない。

    前回の投入がジョブ投入の直前で落ちると、その提出は永久に未採点になる。
    再投入で気づけるようにしてある。
    """
    service, uow, _, _ = backend
    first = accept(service)
    uow.jobs._items.clear()
    uow.jobs._keys.clear()

    second = accept(service)
    assert second.deduplicated
    assert second.submission.id == first.submission.id
    assert uow.jobs.pending_count() == 1


# --------------------------------------------------------------------------
# 再採点
# --------------------------------------------------------------------------


def test_a_regrade_is_a_separate_job(backend) -> None:
    service, uow, _, _ = backend
    result = accept(service)
    regrade = service.request_regrade(
        tenant_id=TENANT, submission_id=result.submission.id, subject_profile=PROFILE
    )

    assert regrade.id != result.job.id
    assert regrade.reason is JobReason.REGRADE
    assert uow.jobs.pending_count() == 2


def test_regrading_twice_with_the_same_discriminator_does_not_pile_up(backend) -> None:
    """モデル更新時の全件再採点は、途中で落ちても安全に再開できる。"""
    service, uow, _, _ = backend
    result = accept(service)
    first = service.request_regrade(
        tenant_id=TENANT,
        submission_id=result.submission.id,
        subject_profile=PROFILE,
        discriminator="model-v2",
    )
    second = service.request_regrade(
        tenant_id=TENANT,
        submission_id=result.submission.id,
        subject_profile=PROFILE,
        discriminator="model-v2",
    )
    assert second.id == first.id
    assert uow.jobs.pending_count() == 2

    third = service.request_regrade(
        tenant_id=TENANT,
        submission_id=result.submission.id,
        subject_profile=PROFILE,
        discriminator="model-v3",
    )
    assert third.id != first.id
    assert uow.jobs.pending_count() == 3


def test_regrading_an_unknown_submission_is_refused(backend) -> None:
    service, _, _, _ = backend
    from aijudge_core.ids import SubmissionId

    with pytest.raises(SubmissionRejected, match="見つかりません"):
        service.request_regrade(
            tenant_id=TENANT,
            submission_id=SubmissionId("sub_" + "f" * 32),
            subject_profile=PROFILE,
        )


# --------------------------------------------------------------------------
# 不変（P8）
# --------------------------------------------------------------------------


def test_a_submitted_submission_cannot_be_overwritten(backend) -> None:
    service, uow, _, _ = backend
    result = accept(service)
    with pytest.raises(ImmutabilityViolation, match="already submitted"):
        uow.submissions.save(result.submission)
