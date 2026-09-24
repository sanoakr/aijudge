"""教員がエディタの作業の記録を見る画面（ADR 0023 §5・設計書 §7）。

固定したいのは次のこと。

教員だけ     TA には開けない。受講していない人の記録は出さない。
見たら残る   一覧も再生も、開いたことが監査ログに残る。
事実だけ     要約と欠けを並べる。記録のない画面・置き場所のない設定でも落ちない。
抜け出せない 学習者のコードに `</script>` があっても、埋め込みから抜け出せない。
一覧で分かる 受付終了時の自動提出は、提出の一覧で本人の提出と見分けられる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import new_id
from aijudge_ide import ActivityFiles, EventBatch, IdeSession, IdeSessionId, snapshot_name

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
START = "int main(void){}\n"


def _record(world: World, learner, root: Path, *, extra_events=(), tasks=()) -> IdeSession:
    session = IdeSession(
        id=IdeSessionId(new_id("ide")),
        tenant_id=world.course.tenant_id,
        learner_id=learner.user_id,
        course_id=world.course.id,
        unit="ex01",
        started_at=NOW,
        consented_at=NOW,
    )
    events = [
        {
            "type": "hello",
            "t": 0,
            "tabs": max(1, len(tasks)),
            "hashes": [snapshot_name(START)],
            "tasks": list(tasks),
        },
        {"type": "edit", "t": 1000, "tab": 0, "off": 15, "del": 0, "ins": "return 0;"},
        {"type": "paste", "t": 2000, "tab": 0, "len": 200, "origin": "external", "hash": "0" * 64},
        {"type": "blur", "t": 3000},
        {"type": "focus", "t": 33_000},
        *extra_events,
    ]
    path, digest, size = ActivityFiles(root).write_batch(
        session, 0, events, {snapshot_name(START): START}
    )
    with world.database.unit_of_work() as uow:
        uow.ide_activity.start_session(session)
        uow.ide_activity.add_batch(
            EventBatch(
                ide_session_id=session.id,
                seq=0,
                received_at=NOW,
                event_count=len(events),
                snapshot_count=1,
                byte_size=size,
                sha256=digest,
                path=path,
            )
        )
        uow.commit()
    return session


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "activity"
    monkeypatch.setenv("AIJUDGE_ACTIVITY_DIR", str(directory))
    return directory


def test_an_instructor_sees_the_summary_and_the_viewing_is_audited(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _record(world, learner, root)

    page = world.client("teacher").get(f"/courses/{world.course.id}/activity/{learner.user_id}")

    assert page.status_code == 200
    assert "1 回・200 字" in page.text  # 外からの貼り付け
    assert "1 回・30 秒" in page.text  # 画面を離れた
    # 200 字の外からの貼り付けには「確かめる場所」の印が付く（判定ではない）。
    assert "大きな外からの貼り付け" in page.text
    assert "不正を意味しません" in page.text
    assert "監査ログに残ります" in page.text
    with world.database.unit_of_work() as uow:
        viewed = [
            e
            for e in uow.audit.list_recent(world.course.tenant_id, limit=20)
            if e.action.value == "activity.viewed"
        ]
    assert len(viewed) == 1
    assert viewed[0].target_id == f"{world.course.id}:{learner.user_id}"


def test_an_assistant_cannot_open_it(world: World, root) -> None:
    world.register("ta", Role.ASSISTANT)
    learner = world.register("s2400001", Role.LEARNER)
    _record(world, learner, root)

    response = world.client("ta").get(f"/courses/{world.course.id}/activity/{learner.user_id}")

    assert response.status_code == 403


def test_someone_not_in_the_course_is_not_found(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    outsider = world.register("stranger", None)

    response = world.client("teacher").get(
        f"/courses/{world.course.id}/activity/{outsider.user_id}"
    )

    assert response.status_code == 404


def test_the_replay_rebuilds_and_cannot_be_escaped(world: World, root) -> None:
    """学習者のコードに `</script>` があっても、埋め込みの JSON から抜け出せない。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    hostile = '"</script><script>alert(1)</script>'
    session = _record(
        world,
        learner,
        root,
        extra_events=[{"type": "edit", "t": 40_000, "tab": 0, "off": 0, "del": 0, "ins": hostile}],
    )

    page = world.client("teacher").get(
        f"/courses/{world.course.id}/activity/{learner.user_id}/{session.id}"
    )

    assert page.status_code == 200
    data = page.text.split('id="replay-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data, "埋め込みの中に < が残っている"
    assert "\\u003c/script>" in data
    with world.database.unit_of_work() as uow:
        details = [
            e.detail
            for e in uow.audit.list_recent(world.course.tenant_id, limit=20)
            if e.action.value == "activity.viewed"
        ]
    assert details == [{"course_id": str(world.course.id), "session_id": str(session.id)}]


def test_another_learners_session_is_not_found_under_this_learner(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    one = world.register("s2400001", Role.LEARNER)
    other = world.register("s2400002", Role.LEARNER)
    session = _record(world, one, root)

    response = world.client("teacher").get(
        f"/courses/{world.course.id}/activity/{other.user_id}/{session.id}"
    )

    assert response.status_code == 404


def test_without_a_configured_place_the_page_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _record(world, learner, tmp_path / "somewhere")
    monkeypatch.delenv("AIJUDGE_ACTIVITY_DIR", raising=False)

    page = world.client("teacher").get(f"/courses/{world.course.id}/activity/{learner.user_id}")

    assert page.status_code == 200
    assert "AIJUDGE_ACTIVITY_DIR" in page.text


def test_an_auto_submission_is_marked_in_the_list(world: World, root) -> None:
    """受付終了時の自動提出は本人が押していない。一覧でそう分かること。"""
    from aijudge_core import ArtifactKind
    from aijudge_ide import SubmissionLink, SubmissionOrigin, content_hash
    from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _import_example(world)
    from aijudge_admin import list_tasks

    task, version = list_tasks(world.database, world.course.id)[0]
    service = SubmissionService(world.database.unit_of_work, FilesystemArtifactStore(root / "a"))
    accepted = service.accept(
        tenant_id=world.course.tenant_id,
        task_version_id=version.id,
        learner_id=learner.user_id,
        subject_profile=version.subject_profile,
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=START.encode())],
    )
    with world.database.unit_of_work() as uow:
        uow.ide_links.record(
            SubmissionLink(
                submission_id=accepted.submission.id,
                tenant_id=world.course.tenant_id,
                learner_id=learner.user_id,
                task_id=task.id,
                origin=SubmissionOrigin.AUTO_CLOSE,
                content_hash=content_hash(START),
                recorded_at=NOW,
            )
        )
        uow.commit()

    teacher_page = world.client("teacher").get(f"/courses/{world.course.id}/submissions").text

    assert "受付終了時の自動提出" in teacher_page
    assert f"/activity/{learner.user_id}" in teacher_page

    world.register("ta", Role.ASSISTANT)
    ta_page = world.client("ta").get(f"/courses/{world.course.id}/submissions").text
    assert "受付終了時の自動提出" in ta_page
    assert f"/activity/{learner.user_id}" not in ta_page


# -- 入口と問題ごとの表示（2026-09-24） --------------------------------------


def _two_problems(world: World) -> list[str]:
    """課題を 2 つ用意し、それぞれの課題版 ID を返す（hello の `tasks` に載せる）。"""
    from aijudge_admin import list_tasks
    from aijudge_core.ids import TaskId, TaskVersionId

    _import_example(world)
    (task, version) = list_tasks(world.database, world.course.id)[0]
    second_task = task.model_copy(update={"id": TaskId("tsk_" + "e" * 32), "title": "第二の問題"})
    second = version.model_copy(
        update={"id": TaskVersionId("tsv_" + "e" * 32), "task_id": second_task.id}
    )
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(second_task)
        uow.tasks.save_version(second)
        uow.commit()
    return [str(version.id), str(second.id)]


def test_the_course_list_shows_every_learner(world: World, root) -> None:
    """**提出していない学習者の記録にも辿れる。** 受講者全員を並べる。"""
    world.register("teacher", Role.INSTRUCTOR)
    wrote = world.register("s2400001", Role.LEARNER)
    world.register("s2400002", Role.LEARNER)
    _record(world, wrote, root)

    page = world.client("teacher").get(f"/courses/{world.course.id}/activity")

    assert page.status_code == 200
    assert "s2400001" in page.text and "s2400002" in page.text
    assert f"/activity/{wrote.user_id}" in page.text
    assert "1 名に記録があります" in page.text


def test_the_course_list_is_for_instructors_only(world: World, root) -> None:
    world.register("ta", Role.ASSISTANT)
    assert world.client("ta").get(f"/courses/{world.course.id}/activity").status_code == 403


def test_the_rail_offers_the_record_to_instructors_only(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    href = f"/courses/{world.course.id}/activity"

    assert href in world.client("teacher").get(f"/courses/{world.course.id}/submissions").text
    assert href not in world.client("ta").get(f"/courses/{world.course.id}/submissions").text


def test_the_learner_page_is_split_by_problem(world: World, root) -> None:
    """1 回のセッションで問題を行き来しても、**問題ごとの節**に分けて数える。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    versions = _two_problems(world)
    _record(
        world,
        learner,
        root,
        tasks=versions,
        extra_events=[
            {"type": "tab", "t": 40_000, "from": 0, "to": 1},
            {"type": "paste", "t": 41_000, "tab": 1, "len": 500, "origin": "external"},
        ],
    )

    page = (
        world.client("teacher").get(f"/courses/{world.course.id}/activity/{learner.user_id}").text
    )

    sections = page.split('class="card activity-problem"')[1:]
    assert len(sections) == 2
    second = next(section for section in sections if "第二の問題" in section)
    first = next(section for section in sections if "第二の問題" not in section)
    # 500 字の貼り付けは第二の問題の節にだけ現れ、再生はその問題を開く。
    assert "500 字" in second and "500 字" not in first
    assert "?tab=1" in second
    # 最初の問題の節には、そちらでの 200 字の貼り付けが出る。
    assert "200 字" in first


def test_a_session_with_no_work_on_a_problem_is_not_listed(world: World, root) -> None:
    """開いただけの回（ハートビートだけ）は、その問題の節に並べない。"""
    from aijudge_ide import ActivityFiles as Files

    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    idle = IdeSession(
        id=IdeSessionId(new_id("ide")),
        tenant_id=world.course.tenant_id,
        learner_id=learner.user_id,
        course_id=world.course.id,
        unit="ex01",
        started_at=NOW,
        consented_at=NOW,
    )
    events = [
        {"type": "hello", "t": 0, "tabs": 1, "hashes": []},
        {"type": "heartbeat", "t": 30_000},
    ]
    path, digest, size = Files(root).write_batch(idle, 0, events, {})
    with world.database.unit_of_work() as uow:
        uow.ide_activity.start_session(idle)
        uow.ide_activity.add_batch(
            EventBatch(
                ide_session_id=idle.id,
                seq=0,
                received_at=NOW,
                event_count=2,
                snapshot_count=0,
                byte_size=size,
                sha256=digest,
                path=path,
            )
        )
        uow.commit()

    page = (
        world.client("teacher").get(f"/courses/{world.course.id}/activity/{learner.user_id}").text
    )

    assert f"/{idle.id}?tab=" not in page
