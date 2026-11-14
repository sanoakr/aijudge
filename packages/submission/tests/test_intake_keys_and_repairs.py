"""冪等キーの範囲と、欠けたジョブの補いを固定する（#686）。

`test_intake.py` が固定しているのは「同じものを二度出しても増えない」側で、
ここで固定するのはその**範囲**と**後始末**である。

課題版   同じ中身でも、課題（版）が違えば別の提出。雛形のままのコードや
         同じ認定証画像、課題を訂正したあとの同一コードの出し直しを、
         前の課題の提出として返すと、新しい課題は未提出のまま締切を迎える。
テナント 明示キーはテナントの中でだけ効く。
補い     提出は残ったがジョブが無い状態を補ったら、**commit する**。
         しないと本番（SQL）では `with` を抜けた時点で巻き戻り、補ったはずの
         ジョブが残らない ── 画面には job が返るので正常に見える。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import ArtifactKind
from aijudge_core.ids import TaskVersionId, TenantId, UserId
from aijudge_submission import (
    FilesystemArtifactStore,
    IncomingFile,
    SubmissionService,
    content_idempotency_key,
    in_memory_backend,
)

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "8" * 32)
TASK = TaskVersionId("tsv_" + "1" * 32)
REVISED_TASK = TaskVersionId("tsv_" + "5" * 32)
LEARNER = UserId("usr_" + "2" * 32)
PROFILE = "cs_lang_c_intro"
START = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path):
    uow, store = in_memory_backend()
    video = FilesystemArtifactStore(tmp_path / "video")
    service = SubmissionService(lambda: uow, store, stream_store=video, clock=lambda: START)
    return service, uow


def template() -> list[IncomingFile]:
    """配布した雛形のまま。別の課題にもそのまま出されうる中身。"""
    return [
        IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(void){}"),
    ]


def accept(service: SubmissionService, **overrides):
    kwargs = {
        "tenant_id": TENANT,
        "task_version_id": TASK,
        "learner_id": LEARNER,
        "subject_profile": PROFILE,
        "files": template(),
    }
    kwargs.update(overrides)
    return service.accept(**kwargs)


def forget_all_jobs(uow) -> None:
    """前回の投入がジョブを積む直前で落ちた状態を作る。"""
    uow.jobs._items.clear()
    uow.jobs._keys.clear()


# --------------------------------------------------------------------------
# 冪等キーの範囲
# --------------------------------------------------------------------------


def test_a_different_task_version_gets_a_different_key() -> None:
    """鍵に課題版が入っていないと、別の課題への提出が前の課題の提出に化ける。"""
    assert content_idempotency_key(TASK, LEARNER, template()) != content_idempotency_key(
        REVISED_TASK, LEARNER, template()
    )


def test_the_same_content_to_two_task_versions_is_two_submissions(backend) -> None:
    """課題を訂正したあとの同一コードの出し直しは、新しい版への提出になる。

    前の版の提出を `deduplicated=True` で返すと、新しい版には提出もジョブも
    作られず、学習者は「提出済み」を見たまま未提出で締切を迎える。
    """
    service, uow = backend
    first = accept(service, task_version_id=TASK)
    second = accept(service, task_version_id=REVISED_TASK)

    assert not second.deduplicated
    assert second.submission.id != first.submission.id
    assert second.submission.task_version_id == REVISED_TASK
    assert second.job.task_version_id == REVISED_TASK
    assert uow.jobs.pending_count() == 2


def test_an_explicit_key_does_not_reach_across_tenants(backend) -> None:
    """同じ明示キー（リクエスト ID）でも、別テナントの提出を返さない。"""
    service, _ = backend
    mine = accept(service, idempotency_key="req-1")
    theirs = accept(service, tenant_id=OTHER_TENANT, idempotency_key="req-1")

    assert not theirs.deduplicated
    assert theirs.submission.id != mine.submission.id


# --------------------------------------------------------------------------
# 補ったら commit する
# --------------------------------------------------------------------------


def test_a_repaired_job_is_committed_on_resubmission(backend) -> None:
    """インメモリでは commit の回数で見る（巻き戻しは SQL 側のテストが見る）。"""
    service, uow = backend
    first = accept(service)
    assert uow.commits == 1
    forget_all_jobs(uow)

    second = accept(service)

    assert second.deduplicated
    assert second.submission.id == first.submission.id
    assert uow.commits == 2


def test_a_plain_duplicate_does_not_commit_again(backend) -> None:
    """何も補わない二重投入は書かない（上の commit が補いの分だけであることの対照）。"""
    service, uow = backend
    accept(service)
    accept(service)
    assert uow.commits == 1


def test_a_repaired_video_job_is_committed(backend) -> None:
    """動画経路の補い（`_existing_for_key`）も commit する。"""
    service, uow = backend

    def stream():
        return service.accept_stream(
            tenant_id=TENANT,
            task_version_id=TASK,
            learner_id=LEARNER,
            subject_profile=PROFILE,
            filename="demo.mp4",
            kind=ArtifactKind.VIDEO,
            chunks=iter([b"same", b"bytes"]),
            max_bytes=1_000_000,
        )

    first = stream()
    assert uow.commits == 1
    forget_all_jobs(uow)

    again = stream()

    assert again.deduplicated
    assert again.submission.id == first.submission.id
    assert uow.jobs.pending_count() == 1
    assert uow.commits == 2
