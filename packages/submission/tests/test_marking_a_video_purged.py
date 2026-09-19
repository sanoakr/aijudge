"""保存期間を過ぎた動画に印を付ける規則（ADR 0020）。インメモリ実装側。

固定したいこと:

印     ファイルを消したことが `Artifact.purged_at` に残り、行は消えない。
不変   提出そのものは書き換えない（`save` の規則は緩めていない）。
冪等   二度目の消去で時刻を後ろへずらさない。
範囲   指名していない成果物には触らない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import ArtifactKind
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import (
    FilesystemArtifactStore,
    ImmutabilityViolation,
    SubmissionService,
    in_memory_backend,
)

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PURGED_AT = datetime(2027, 4, 1, 3, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path):
    uow, store = in_memory_backend()
    video = FilesystemArtifactStore(tmp_path / "video")
    service = SubmissionService(lambda: uow, store, stream_store=video, clock=lambda: START)
    return service, uow, video


def a_video(service, *, filename: str = "demo.mp4", body: bytes = b"BODY"):
    return service.accept_stream(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile="report_ja",
        filename=filename,
        kind=ArtifactKind.VIDEO,
        chunks=iter((b"MP4", body)),
        max_bytes=1_000_000,
    )


def test_the_row_stays_and_carries_the_time_it_was_purged(backend) -> None:
    service, uow, _ = backend
    submission = a_video(service).submission
    (artifact,) = submission.artifacts

    marked = uow.submissions.mark_artifacts_purged(
        [(submission.id, artifact.id)], purged_at=PURGED_AT
    )

    assert marked == 1
    loaded = uow.submissions.get(submission.id)
    assert loaded is not None
    (after,) = loaded.artifacts
    assert after.is_purged is True
    assert after.purged_at == PURGED_AT
    # **消したのはファイルだけ。** その採点が何を見て付いたかは読めるまま。
    assert after.storage_key == artifact.storage_key
    assert after.content_hash == artifact.content_hash
    assert after.byte_size == artifact.byte_size


def test_the_submission_itself_is_still_immutable(backend) -> None:
    """印を付ける経路があっても、`save` の規則は緩めていない（P8）。"""
    service, uow, _ = backend
    submission = a_video(service).submission
    with pytest.raises(ImmutabilityViolation):
        uow.submissions.save(submission)


def test_purging_twice_keeps_the_first_time(backend) -> None:
    service, uow, _ = backend
    submission = a_video(service).submission
    (artifact,) = submission.artifacts
    uow.submissions.mark_artifacts_purged([(submission.id, artifact.id)], purged_at=PURGED_AT)

    later = datetime(2027, 9, 1, 3, 0, tzinfo=UTC)
    marked = uow.submissions.mark_artifacts_purged([(submission.id, artifact.id)], purged_at=later)

    # 数に入らないので、2 度目の実行が「また N 件消した」と報告しない。
    assert marked == 0
    loaded = uow.submissions.get(submission.id)
    assert loaded is not None
    assert loaded.artifacts[0].purged_at == PURGED_AT


def test_other_submissions_are_left_alone(backend) -> None:
    service, uow, _ = backend
    # **中身を変える。** 同じ内容の再投入は冪等に同じ提出を返すので
    # （`test_stream_intake`）、ファイル名だけ変えても 2 件にならない。
    first = a_video(service, filename="one.mp4", body=b"ONE").submission
    second = a_video(service, filename="two.mp4", body=b"TWO").submission

    uow.submissions.mark_artifacts_purged([(first.id, first.artifacts[0].id)], purged_at=PURGED_AT)

    untouched = uow.submissions.get(second.id)
    assert untouched is not None
    assert untouched.artifacts[0].is_purged is False
