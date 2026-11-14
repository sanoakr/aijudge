"""動画経路も、出した人の役割・試用・接続元を提出に焼き付ける（#686）。

ファイル経路（`accept`）と同じ 3 つ ── `submitted_as`・`is_demo`・`source_ip` ──
を、動画経路（`accept_stream` / `record_streamed`）でも落とさないことを固定する。
落ちると、教員・TA が動画課題に出した動作確認が学習者の提出として記録され
（`is_trial` が偽）、成績・統計・未確定件数に数えられる。`_enqueue` の
「教員の提出は採点を保留しない」も効かなくなる。

あわせて、上限の**境界**を固定する。ちょうど上限の大きさは受け付け、
1 バイトでも超えたら断る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_core import ArtifactKind, Role
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, TenantId, UserId
from aijudge_submission import (
    FilesystemArtifactStore,
    SubmissionService,
    SubmissionTooLarge,
    in_memory_backend,
)
from aijudge_submission.protocols import artifact_storage_key

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
INSTRUCTOR = UserId("usr_" + "3" * 32)
PROFILE = "report_ja"
START = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
# 試験の採点開始（#67）。学習者の提出はここまで寝かせる。
GRADING_STARTS_AT = START + timedelta(hours=2)
ADDRESS = "192.0.2.10"


@pytest.fixture
def backend(tmp_path: Path):
    uow, store = in_memory_backend()
    video = FilesystemArtifactStore(tmp_path / "video")
    service = SubmissionService(lambda: uow, store, stream_store=video, clock=lambda: START)
    return service, uow, video


def stream(service: SubmissionService, chunks=(b"MP4", b"BODY"), **over):
    kwargs = dict(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=INSTRUCTOR,
        subject_profile=PROFILE,
        filename="demo.mp4",
        kind=ArtifactKind.VIDEO,
        chunks=iter(chunks),
        max_bytes=1_000_000,
    )
    kwargs.update(over)
    return service.accept_stream(**kwargs)


# --------------------------------------------------------------------------
# 役割・試用・接続元
# --------------------------------------------------------------------------


def test_a_streamed_submission_keeps_the_role_demo_flag_and_address(backend) -> None:
    service, uow, _ = backend
    result = stream(service, submitted_as=Role.INSTRUCTOR, is_demo=True, source_ip=ADDRESS)

    stored = uow.submissions.get(result.submission.id)
    assert stored is not None
    assert stored.submitted_as is Role.INSTRUCTOR
    assert stored.is_demo is True
    assert stored.source_ip == ADDRESS
    # 測定から外れる（成績にも未確定件数にも数えない）のはこの値の帰結。
    assert stored.is_trial


def test_record_streamed_keeps_them_too(backend) -> None:
    """分割アップロードの確定（Web ルートが書いた blob から作る）も同じ。"""
    service, uow, video = backend
    submission_id = SubmissionId("sub_" + "6" * 32)
    artifact_id = ArtifactId("art_" + "7" * 32)
    key = artifact_storage_key(TENANT, submission_id, artifact_id, "demo.mp4")
    blob = video.put_stream(key, iter([b"video-bytes"]))

    result = service.record_streamed(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=INSTRUCTOR,
        subject_profile=PROFILE,
        filename="demo.mp4",
        kind=ArtifactKind.VIDEO,
        submission_id=submission_id,
        artifact_id=artifact_id,
        storage_key=key,
        byte_size=blob.byte_size,
        sha256=blob.sha256,
        submitted_as=Role.ASSISTANT,
        is_demo=True,
        source_ip=ADDRESS,
    )

    stored = uow.submissions.get(result.submission.id)
    assert stored is not None
    assert (stored.submitted_as, stored.is_demo, stored.source_ip) == (
        Role.ASSISTANT,
        True,
        ADDRESS,
    )


def test_an_instructors_video_is_not_held_until_grading_starts(backend) -> None:
    """教員・TA の提出は採点を保留しない（`_enqueue`）。動画経路でも同じ。"""
    service, _, _ = backend
    result = stream(service, submitted_as=Role.INSTRUCTOR, grading_starts_at=GRADING_STARTS_AT)
    assert result.job.available_at == START


def test_a_learners_video_is_held_until_grading_starts(backend) -> None:
    """上の対照。学習者の提出は採点開始まで寝かせる。"""
    service, _, _ = backend
    result = stream(service, grading_starts_at=GRADING_STARTS_AT)
    assert result.submission.submitted_as is Role.LEARNER
    assert result.job.available_at == GRADING_STARTS_AT


# --------------------------------------------------------------------------
# 上限の境界
# --------------------------------------------------------------------------


def test_a_stream_of_exactly_the_limit_is_accepted(backend) -> None:
    service, _, video = backend
    result = stream(service, [b"1234", b"5678"], max_bytes=8)
    assert result.submission.artifacts[0].byte_size == 8
    assert video.get(result.submission.artifacts[0].storage_key) == b"12345678"


def test_one_byte_over_the_limit_is_refused(backend) -> None:
    service, uow, _ = backend
    with pytest.raises(SubmissionTooLarge):
        stream(service, [b"1234", b"56789"], max_bytes=8)
    assert uow.submissions.list_for_learner(TENANT, INSTRUCTOR, TASK) == ()
