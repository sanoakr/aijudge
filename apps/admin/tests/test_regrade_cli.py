"""確定済みも含めて課題を採点し直す CLI（2026-10-17）。

固定したいのは 5 つ。

下見を先に   確定を開け直す件数と、人の採点が効かなくなる件数を出してから訊く
確定も積む   コンソールの再採点と違い、確定済み・人が採点済みの提出も積む
成績にしない  動作確認の提出は積まない（#108）
二度積まない  同じ状態で二度流しても、ジョブは増えない
理由を残す    理由の無い操作は断り、何件・なぜを監査ログに残す
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import test_finalization
from test_finalization import (
    DUE,
    INSTRUCTOR,
    PROFILES,
    TASK_ID,
    TASK_VERSION,
    TENANT,
    _task_version,
    _world,
)

from aijudge_admin.cli import main
from aijudge_audit import AuditAction
from aijudge_core import (
    Finalization,
    FinalizationSource,
    HumanReview,
    ReviewState,
    Routing,
    Task,
    new_id,
)
from aijudge_core.ids import FinalizationId, HumanReviewId, TaskId, TaskVersionId
from aijudge_course_admin.operations import AdminError, ensure_course
from aijudge_course_admin.regrade import find_task
from aijudge_persistence import Database
from aijudge_submission import JobReason

REASON = "画像を読むモデルの不具合で書き起こしが打ち切られたため"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/regrade.db"


def _approved():
    """承認済みの版。**再採点は学習者に出ている版で走らせる**（承認待ちでは断る・#48）。"""
    version = _task_version()
    return version.model_copy(
        update={
            "provenance": version.provenance.model_copy(
                update={"review_state": ReviewState.APPROVED}
            )
        }
    )


@pytest.fixture
def world(db_url: str, monkeypatch: pytest.MonkeyPatch):
    """提出 4 件。0 は自動確定、1 は教員が採点して確定、2 は未確定、3 は教員の動作確認。"""
    monkeypatch.setattr(test_finalization, "_task_version", _approved)
    database = Database.connect(db_url, create=True)
    try:
        course, _ = ensure_course(
            database,
            tenant_id=TENANT,
            code="network",
            title="ネットワーク及び演習",
            term="2026-後期",
            subject_profile="cs_lang_c_intro",
            profiles_dir=PROFILES,
        )
        ids = _world(database, course.id, routings=(Routing.AUTO,) * 4, trial_at=3)
        with database.unit_of_work() as uow:
            run0 = uow.runs.latest_for(ids[0])
            run1 = uow.runs.latest_for(ids[1])
            uow.reviews.save_finalization(
                Finalization(
                    id=FinalizationId(new_id("fin")),
                    grading_run_id=run0.id,
                    source=FinalizationSource.AUTOMATIC,
                    justification="締切後の猶予が過ぎたので確定しました。",
                    finalized_at=DUE,
                )
            )
            review = HumanReview(
                id=HumanReviewId(new_id("hrv")),
                grading_run_id=run1.id,
                grader_id=INSTRUCTOR,
                comment="画像を見て段階を確かめました。",
                reviewed_at=DUE,
            )
            uow.reviews.save_review(review)
            uow.reviews.save_finalization(
                Finalization(
                    id=FinalizationId(new_id("fin")),
                    grading_run_id=run1.id,
                    source=FinalizationSource.INSTRUCTOR_REVIEW,
                    actor_id=INSTRUCTOR,
                    review_id=review.id,
                    justification="画像を見て段階を確かめました。",
                    finalized_at=DUE,
                )
            )
            uow.commit()
    finally:
        database.dispose()
    return course, ids


def _cli(db_url: str, tmp_path: Path, *args: str) -> int:
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            str(TENANT),
            "--profiles",
            str(PROFILES),
            "--artifacts",
            str(tmp_path / "artifacts"),
            *args,
        ]
    )


def _regrade_jobs(db_url: str, ids) -> int:
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            return sum(
                1
                for submission_id in ids
                for job in _jobs_for(uow, submission_id)
                if job.reason is JobReason.REGRADE
            )
    finally:
        database.dispose()


def _jobs_for(uow, submission_id):
    """提出 1 件のジョブ。SQL の実装に一覧の口が無いので、冪等キーで引く。"""
    run = uow.runs.latest_for(submission_id)
    key = f"{submission_id}|regrade|admin-regrade:{run.id}:{TASK_VERSION}"
    job = uow.jobs.find_by_idempotency_key(key)
    return () if job is None else (job,)


def test_the_preview_counts_what_will_be_reopened_and_asks_first(
    db_url: str, tmp_path: Path, world, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    course, ids = world
    # 答えが得られないので確認は「いいえ」になる（`_confirmed`）。
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code = _cli(
        db_url,
        tmp_path,
        "regrade",
        "--course",
        str(course.id),
        "--task",
        str(TASK_ID),
        "--reason",
        REASON,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "中止しました" in out
    assert "採点し直す提出      3 件" in out, "動作確認の提出は数えない"
    assert "うち確定済み        2 件" in out
    assert "うち人の採点あり    1 件" in out
    assert _regrade_jobs(db_url, ids) == 0, "確認の前に積んだ"


def test_yes_queues_finalized_and_reviewed_submissions_too(
    db_url: str, tmp_path: Path, world, capsys
) -> None:
    course, ids = world
    code = _cli(
        db_url,
        tmp_path,
        "regrade",
        "--course",
        str(course.id),
        "--task",
        str(TASK_ID),
        "--reason",
        REASON,
        "--yes",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "積みました" in out and "3 件" in out
    # 教員が採点していた提出は、あとで見比べられるよう id を出す。
    assert str(ids[1]) in out
    assert _regrade_jobs(db_url, ids[:3]) == 3
    assert _regrade_jobs(db_url, ids[3:]) == 0, "動作確認の提出を積んだ"

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            (event,) = uow.audit.list_recent(TENANT, action=AuditAction.GRADES_REGRADED)
    finally:
        database.dispose()
    assert event.target_id == str(TASK_ID)
    assert event.detail["reason"] == REASON
    assert event.detail["finalized"] == 2 and event.detail["reviewed"] == 1


def test_running_it_twice_queues_nothing_new(db_url: str, tmp_path: Path, world, capsys) -> None:
    course, _ = world
    args = ("regrade", "--course", str(course.id), "--task", str(TASK_ID), "--reason", REASON)
    assert _cli(db_url, tmp_path, *args, "--yes") == 0
    assert _cli(db_url, tmp_path, *args, "--yes") == 0

    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            pending = uow.jobs.pending_count()
    finally:
        database.dispose()
    # 提出時の 4 件 + 再採点 3 件。二度目は同じ冪等キーなので増えない。
    assert pending == 4 + 3


def test_a_short_reason_is_refused(db_url: str, tmp_path: Path, world, capsys) -> None:
    course, ids = world
    code = _cli(
        db_url,
        tmp_path,
        "regrade",
        "--course",
        str(course.id),
        "--task",
        str(TASK_ID),
        "--reason",
        "再採点",
        "--yes",
    )
    assert code == 2
    assert "10 文字以上" in capsys.readouterr().err
    assert _regrade_jobs(db_url, ids) == 0


def test_a_task_is_found_by_its_key(db_url: str, world) -> None:
    course, _ = world
    other = TaskId("tsk_" + "8" * 32)
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=other, course_id=course.id, title="ex4 paiza4"))
            uow.tasks.save_version(
                _task_version().model_copy(
                    update={
                        "id": TaskVersionId("tsv_" + "8" * 32),
                        "task_id": other,
                        "source_key": "ex4/paiza4",
                    }
                )
            )
            uow.commit()
        assert find_task(database, course_id=course.id, task="ex4/paiza4").id == other
        with pytest.raises(AdminError, match="ありません"):
            find_task(database, course_id=course.id, task="ex9/none")
    finally:
        database.dispose()
