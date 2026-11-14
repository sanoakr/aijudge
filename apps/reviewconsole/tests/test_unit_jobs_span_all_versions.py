"""問題セットの「いま採点する」「流し直す」と待機件数は、**課題の全版への提出**を見る（#694）。

提出は受付時の課題版を持つ。課題を訂正して版が上がると、訂正前の提出は古い版を
指したまま残る。最新版の提出だけを数えると、試験中に問題文を直したあとで
「いま採点する」を押しても、それまでの提出は 0 件と表示され、採点開始時刻まで
動かなかった。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_manage import TENANT, World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import ArtifactKind, GradingPhase, Role
from aijudge_core.ids import TaskId
from aijudge_reviewconsole.notices import JOBS_RELEASED

LATER = timedelta(hours=2)


def _add_task(world: World, client, *, unit: str, suffix: str) -> str:
    with world.database.unit_of_work() as uow:
        before = {t.id for t in uow.tasks.list_for_course(world.course.id)}
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": suffix,
            "unit": unit,
            "statement": f"## [必須] 課題 {suffix} ##\n\n本文",
            "readability_weight": "0.3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with world.database.unit_of_work() as uow:
        (added,) = [t for t in uow.tasks.list_for_course(world.course.id) if t.id not in before]
    return str(added.id)


def _submit_waiting(world: World, learner, task_id: str):
    """採点開始を先に置いて 1 件出す（ジョブは積まれるが、その時刻まで動かない）。"""
    from aijudge_submission import IncomingFile, SubmissionService

    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    return SubmissionService(world.database.unit_of_work, world.console.store).accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner.user_id,
        subject_profile=version.subject_profile,
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
        grading_starts_at=datetime.now(UTC) + LATER,
    )


def _revise(client, world: World, task_id: str) -> None:
    """問題文を直して版を上げる（訂正前の提出は古い版を指したまま）。"""
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 直した ##\n\n誤字を直した本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text
    with world.database.unit_of_work() as uow:
        versions = uow.tasks.versions_for_tasks([TaskId(task_id)])
    assert len(versions) == 2


def test_grade_now_releases_a_submission_to_an_older_version(world: World) -> None:
    teacher = world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")
    accepted = _submit_waiting(world, learner, task_id)
    _revise(client, world, task_id)
    now = datetime.now(UTC)
    with world.database.unit_of_work() as uow:
        assert uow.jobs.waiting_count([accepted.submission.id], now) == 1

    page = client.get(f"/manage/courses/{world.course.id}/units/ex01")
    response = client.post(
        f"/manage/courses/{world.course.id}/units/ex01/grade-now", follow_redirects=False
    )

    assert page.status_code == 200
    # 押す前の画面の件数にも、訂正前の提出が入っている
    assert "いま待機している提出は 1 件です" in page.text
    assert response.status_code == 303
    assert world.console.notices.take(teacher.user_id, world.course.id, JOBS_RELEASED) == 1
    with world.database.unit_of_work() as uow:
        assert uow.jobs.waiting_count([accepted.submission.id], datetime.now(UTC)) == 0


def test_a_failed_job_on_an_older_version_is_listed_and_put_back(world: World) -> None:
    teacher = world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")
    accepted = _submit_waiting(world, learner, task_id)
    later = datetime.now(UTC) + LATER + timedelta(minutes=1)
    with world.database.unit_of_work() as uow:
        job = uow.jobs.reserve(
            later, worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC
        )
        assert job is not None and job.submission_id == accepted.submission.id
        uow.jobs.update(job.failed(later, "boom", permanent=True))
        uow.commit()
    _revise(client, world, task_id)

    page = client.get(f"/manage/courses/{world.course.id}/units/ex01")
    response = client.post(
        f"/manage/courses/{world.course.id}/units/ex01/retry-failed", follow_redirects=False
    )

    assert page.status_code == 200
    assert "採点に失敗した提出が 1 件あります" in page.text
    assert response.status_code == 303
    assert world.console.notices.take(teacher.user_id, world.course.id, JOBS_RELEASED) == 1
    with world.database.unit_of_work() as uow:
        assert not uow.jobs.failed_for([accepted.submission.id])
