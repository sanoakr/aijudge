"""同じ内容の提出が同時に 2 本届いても、後の側が 500 にならない（2026-11-14・#679）。

ブラウザの二度押し・POST の再送・自動提出と本人の提出の重なりでは、2 本のリクエストが
どちらも「この冪等キーはまだ無い」を見たあと、一意制約が後から来た側を止める。以前は例外が
そのまま上がって 500 になり、提出は成立しているのに学習者にはエラー画面が出て、先に置いた
成果物ファイルは DB から参照されないまま残った。

固定したいのは 3 つ。

勝った側を返す  負けた側は例外を上げず、先に成立した提出とジョブを返す（`deduplicated`）
孤児を残さない  負けた側が置いた成果物は消える
増やさない      提出もジョブもイベントも 1 件のまま
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import ArtifactKind
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import IncomingFile, SubmissionService, in_memory_backend

TENANT = TenantId("ten_" + "0" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _files() -> list[IncomingFile]:
    return [IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}\n")]


def _accept(service: SubmissionService):
    return service.accept(
        tenant_id=TENANT,
        task_version_id=TASK,
        learner_id=LEARNER,
        subject_profile="cs_lang_c_intro",
        files=_files(),
    )


@pytest.fixture
def backend():
    uow, store = in_memory_backend()
    service = SubmissionService(lambda: uow, store, clock=lambda: NOW)
    return service, uow, store


def _lose_the_race(uow, monkeypatch) -> None:
    """1 回目の `find_by_idempotency_key` だけ「まだ無い」を返す（両方が無いを見た形）。"""
    real = uow.submissions.find_by_idempotency_key
    calls = {"n": 0}

    def once_blind(tenant_id, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(tenant_id, key)

    monkeypatch.setattr(uow.submissions, "find_by_idempotency_key", once_blind)


def test_the_loser_returns_the_winning_submission(backend, monkeypatch) -> None:
    service, uow, _ = backend
    winner = _accept(service)

    _lose_the_race(uow, monkeypatch)
    loser = _accept(service)

    assert loser.deduplicated
    assert loser.submission.id == winner.submission.id
    assert loser.job.id == winner.job.id


def test_the_loser_leaves_no_orphan_artifact(backend, monkeypatch) -> None:
    service, uow, store = backend
    winner = _accept(service)
    before = set(store._blobs)

    _lose_the_race(uow, monkeypatch)
    _accept(service)

    assert set(store._blobs) == before, "負けた側の成果物が残っている"
    assert {a.storage_key for a in winner.submission.artifacts} == before


def test_the_loser_adds_nothing(backend, monkeypatch) -> None:
    service, uow, _ = backend
    _accept(service)

    _lose_the_race(uow, monkeypatch)
    _accept(service)

    assert uow.jobs.pending_count() == 1
    assert len(uow.outbox.all_events()) == 1
    assert len(uow.submissions.list_for_learner(TENANT, LEARNER, TASK)) == 1


def test_a_streamed_upload_that_loses_the_race_returns_the_winner_and_drops_its_blob(
    tmp_path, monkeypatch
) -> None:
    """動画の受付（`record_streamed`）も同じ。書いた blob は消して、勝った側を返す。"""
    from aijudge_core.ids import ArtifactId, SubmissionId
    from aijudge_submission import FilesystemArtifactStore

    uow, store = in_memory_backend()
    video = FilesystemArtifactStore(tmp_path / "video")
    service = SubmissionService(lambda: uow, store, stream_store=video, clock=lambda: NOW)

    def record(tag: str):
        key = f"video/{tag}.mp4"
        video.put(key, b"MP4BODY")
        return service.record_streamed(
            tenant_id=TENANT,
            task_version_id=TASK,
            learner_id=LEARNER,
            subject_profile="cs_lang_c_intro",
            filename="demo.mp4",
            kind=ArtifactKind.VIDEO,
            submission_id=SubmissionId("sub_" + tag * 32),
            artifact_id=ArtifactId("art_" + tag * 32),
            storage_key=key,
            byte_size=7,
            sha256="ab" * 32,
        ), key

    winner, winner_key = record("a")
    _lose_the_race(uow, monkeypatch)
    loser, loser_key = record("b")

    assert loser.deduplicated
    assert loser.submission.id == winner.submission.id
    assert video.exists(winner_key)
    assert not video.exists(loser_key), "負けた側の blob が残っている"
