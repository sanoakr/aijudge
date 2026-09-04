"""動画のストリーム受付の規則を固定する。

固定したいこと:

作る   ストリームで書いた blob から提出・ジョブ・イベントが同時に成立する。
上限   `max_bytes` を超えたら EOF を待たず打ち切り、ファイルを残さない。
冪等   `Idempotency-Key` と内容ハッシュのどちらでも、二重投入で提出が増えない。
掃除   提出を作れなければ書いた blob を残さない（孤児を作らない）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import ArtifactKind, SubmissionState
from aijudge_core.events import SubmissionCreated
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import (
    FilesystemArtifactStore,
    SubmissionRejected,
    SubmissionService,
    SubmissionTooLarge,
    in_memory_backend,
)

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
PROFILE = "report_ja"
START = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path):
    uow, store = in_memory_backend()
    video = FilesystemArtifactStore(tmp_path / "video")
    service = SubmissionService(lambda: uow, store, stream_store=video, clock=lambda: START)
    return service, uow, video


def stream(service, chunks=(b"MP4", b"BODY", b"HERE"), *, filename="demo.mp4", **over):
    kwargs = dict(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile=PROFILE,
        filename=filename,
        kind=ArtifactKind.VIDEO,
        chunks=iter(chunks),
        max_bytes=1_000_000,
    )
    kwargs.update(over)
    return service.accept_stream(**kwargs)


def test_stream_creates_submission_job_and_event(backend) -> None:
    service, uow, video = backend
    result = stream(service)

    assert result.deduplicated is False
    sub = uow.submissions.get(result.submission.id)
    assert sub is not None
    assert sub.state is SubmissionState.SUBMITTED
    (artifact,) = sub.artifacts
    assert artifact.kind is ArtifactKind.VIDEO
    assert artifact.byte_size == len(b"MP4BODYHERE")
    assert artifact.content_hash.startswith("sha256:")
    assert video.get(artifact.storage_key) == b"MP4BODYHERE"

    assert result.job is not None  # ジョブが積まれている
    events = [e for e in uow.outbox.all_events() if isinstance(e, SubmissionCreated)]
    assert [e.submission_id for e in events] == [result.submission.id]


def test_oversize_is_cut_off_and_leaves_no_file(backend) -> None:
    service, uow, video = backend
    big = (b"x" * 400 for _ in range(100))  # 40 KB
    with pytest.raises(SubmissionTooLarge):
        stream(service, big, max_bytes=1000)
    # 提出は作られていない。
    assert uow.submissions.list_for_learner(TENANT, LEARNER, TASK) == ()
    # 中途半端なファイルも残っていない。
    assert list((video.root).rglob("*.mp4")) == []
    assert list((video.root).rglob("*.partial")) == []


def test_empty_stream_is_rejected(backend) -> None:
    service, _uow, video = backend
    with pytest.raises(SubmissionRejected):
        stream(service, [b"", b""])
    assert list(video.root.rglob("*.mp4")) == []


def test_idempotency_key_dedupes_before_reading_body(backend) -> None:
    service, _uow, _video = backend
    first = stream(service, idempotency_key="upload-123")

    # 2 回目は本文を読まずに既存を返す（チャンクを列挙したら失敗する generator）。
    def exploding():
        raise AssertionError("body should not be read on a known Idempotency-Key")
        yield b""

    again = service.accept_stream(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile=PROFILE,
        filename="demo.mp4",
        kind=ArtifactKind.VIDEO,
        chunks=exploding(),
        max_bytes=1_000_000,
        idempotency_key="upload-123",
    )
    assert again.deduplicated is True
    assert again.submission.id == first.submission.id


def test_identical_content_without_key_does_not_make_a_new_attempt(backend) -> None:
    service, _uow, video = backend
    first = stream(service, [b"same", b"bytes"])
    again = stream(service, [b"same", b"bytes"])
    assert again.deduplicated is True
    assert again.submission.id == first.submission.id
    # 2 度目に書いた blob は消えている（最初の 1 個だけ）。
    assert len(list(video.root.rglob("*.mp4"))) == 1


def test_no_stream_store_configured_is_rejected() -> None:
    uow, store = in_memory_backend()
    service = SubmissionService(lambda: uow, store, clock=lambda: START)
    with pytest.raises(SubmissionRejected):
        service.accept_stream(
            tenant_id=TENANT,
            task_version_id=TASK,
            learner_id=LEARNER,
            subject_profile=PROFILE,
            filename="demo.mp4",
            kind=ArtifactKind.VIDEO,
            chunks=iter([b"x"]),
            max_bytes=10,
        )
