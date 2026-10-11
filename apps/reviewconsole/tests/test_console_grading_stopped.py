"""採点が止まっていることを画面が隠さない（2026-10-11）。

prog2 の採点が 6 時間全件失敗したとき、画面からは分からなかった。

- コースの採点設定は、保存された上書きが壊れていると雛形の値で欄を埋めるので、
  印が付いて見えて「保存されている」と読めた（実際は採点が全件失敗していた）
- 提出の一覧は、採点の無い提出を一律に「採点中」と出していた
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_manage import TENANT, World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import ArtifactKind, GradingPhase, Role
from aijudge_submission import IncomingFile, SubmissionService


def test_broken_course_settings_are_announced(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(
            course.model_copy(update={"grading_overrides": {"deterministic": ["does_not_exist"]}})
        )
        uow.commit()

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert "このコースの採点はすべて失敗しています" in page
    assert "does_not_exist" in page


def test_sound_course_settings_say_nothing(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text
    assert "採点はすべて失敗しています" not in page


def test_a_submission_whose_grading_failed_is_not_shown_as_grading(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": "p1",
            "unit": "ex01",
            "statement": "## 課題 ##\n\n本文",
            "position": "1",
            "readability_weight": "0.3",
        },
    )
    with world.database.unit_of_work() as uow:
        (task,) = uow.tasks.list_for_course(world.course.id)
        version = uow.tasks.latest_version(task.id)
    service = SubmissionService(world.database.unit_of_work, world.console.store)
    service.accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner.user_id,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
    )
    page = client.get(f"/courses/{world.course.id}/submissions").text
    assert "採点中" in page and "採点に失敗" not in page

    now = datetime.now(UTC)
    with world.database.unit_of_work() as uow:
        job = uow.jobs.reserve(now, worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC)
        assert job is not None
        uow.jobs.update(job.failed(now, "コースの採点設定が不正です", permanent=True))
        uow.commit()

    page = client.get(f"/courses/{world.course.id}/submissions").text
    assert "採点に失敗" in page
