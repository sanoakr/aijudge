"""テストの薄い経路の、**いまの振る舞いを写す**（段階的な立て直し・段階 0-3a）。

`docs/design/staged-refactor.md` の約束 3: 移す前に、そこを守るテストがあること。
2026-09-26 の調査で、テストからの参照が 0〜1 回の経路があった。`manage.py` を
画面ごとに分ける（段階 4）と、これらは黙って壊れうる。ここに書くのは**仕様では
なく、いまの応答**である（状態コード・保存されるもの・行き先）── 変えるときは、
意図して変えたことをこのテストの書き換えで示す。

AI を使う経路（作問・候補・参照解答・入力の提案・シラバスの読み取り）は 0-3b。
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from test_manage import TENANT, World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import ArtifactKind, GradingPhase, Role
from aijudge_core.ids import TaskId


def _add_task(world: World, client, *, unit: str, suffix: str, position: str = "") -> str:
    with world.database.unit_of_work() as uow:
        before = {t.id for t in uow.tasks.list_for_course(world.course.id)}
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks",
        data={
            "key_suffix": suffix,
            "unit": unit,
            "statement": f"## [必須] 課題 {suffix} ##\n\n本文",
            "readability_weight": "0.3",
            "position": position,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    with world.database.unit_of_work() as uow:
        (added,) = [t for t in uow.tasks.list_for_course(world.course.id) if t.id not in before]
    return str(added.id)


def _submit(world: World, learner, task_id: str):
    from aijudge_submission import IncomingFile, SubmissionService

    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    return SubmissionService(world.database.unit_of_work, world.console.store).accept(
        tenant_id=TENANT,
        task_version_id=version.id,
        learner_id=learner.user_id,
        subject_profile=version.subject_profile,
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=b"int main(){}")],
    )


def _versions(world: World, task_id: str):
    with world.database.unit_of_work() as uow:
        return sorted(
            uow.tasks.versions_for_tasks([TaskId(task_id)]), key=lambda version: version.version
        )


# --------------------------------------------------------------------------
# 課題の版を戻す（`tasks/{id}/restore`）── 参照 0 回
# --------------------------------------------------------------------------


def test_restoring_an_old_version_makes_a_new_version_with_its_content(world: World) -> None:
    """**版は書き換えない**（P8）。古い版の中身で新しい版を作る。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")
    client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": "## [必須] 直した ##\n\n別の本文", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    first, second = _versions(world, task_id)
    assert second.statement != first.statement

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/restore",
        data={"version_id": str(first.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert f"/tasks/{task_id}/edit" in response.headers["location"]
    versions = _versions(world, task_id)
    assert [v.version for v in versions] == [1, 2, 3]
    assert versions[-1].statement == first.statement
    assert versions[0].statement == first.statement, "古い版が書き換わった"


def test_a_version_of_another_task_is_not_restored(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    mine = _add_task(world, client, unit="ex01", suffix="p1")
    other = _add_task(world, client, unit="ex01", suffix="p2")
    (other_version,) = _versions(world, other)

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{mine}/restore",
        data={"version_id": str(other_version.id)},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert len(_versions(world, mine)) == 1


# --------------------------------------------------------------------------
# 名簿を消す（`groups/delete`）── 参照 0 回
# --------------------------------------------------------------------------


def _make_group(world: World, client, name: str) -> None:
    world.register("s2400001", Role.LEARNER)
    response = client.post(
        f"/manage/courses/{world.course.id}/groups",
        data={"name": name, "members": "s2400001"},
    )
    assert response.status_code == 200, response.text


def _group_names(world: World) -> list[str]:
    from aijudge_admin import groups as audience

    with world.database.unit_of_work() as uow:
        return [row.group.name for row in audience.list_groups(uow, world.course)]


def test_an_unused_group_is_deleted(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _make_group(world, client, "追試")

    response = client.post(
        f"/manage/courses/{world.course.id}/groups/delete",
        data={"name": "追試"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/groups?saved=group_deleted")
    assert _group_names(world) == []


def test_a_group_in_use_as_an_audience_is_kept(world: World) -> None:
    """**出題先として使われていれば消さない**（先に出題先から外す）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _import_example(world)
    unit = _unit_of(world)
    _make_group(world, client, "追試")
    client.post(
        f"/manage/courses/{world.course.id}/units/{unit}/audience",
        data={"groups": "追試"},
        follow_redirects=False,
    )

    response = client.post(
        f"/manage/courses/{world.course.id}/groups/delete",
        data={"name": "追試"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert _group_names(world) == ["追試"]


# --------------------------------------------------------------------------
# 画面の静止画の切り替え（`units/{unit}/screen-capture`）── 参照 0 回
# --------------------------------------------------------------------------


def test_screen_capture_is_switched_for_the_whole_unit(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    first = _add_task(world, client, unit="exam", suffix="q1")
    second = _add_task(world, client, unit="exam", suffix="q2")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/exam/screen-capture",
        data={"screen_capture": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "saved=screen_capture" in response.headers["location"]
    with world.database.unit_of_work() as uow:
        assert all(uow.tasks.get_task(TaskId(t)).screen_capture for t in (first, second))
        events = uow.audit.list_recent(world.course.tenant_id, limit=20)
    assert any(e.detail.get("field") == "screen_capture" for e in events)


# --------------------------------------------------------------------------
# 並べ替え・再採点・削除・片付け・流し直し ── 参照 1 回
# --------------------------------------------------------------------------


def test_moving_the_last_task_down_changes_nothing(world: World) -> None:
    """末尾をさらに下へは動かさない（入れ替える相手がいない）。**位置を明示する** ──
    位置の無い課題どうしは並びの鍵が同点で、どちらが末尾かが決まらない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    first = _add_task(world, client, unit="ex04", suffix="p1", position="1")
    last = _add_task(world, client, unit="ex04", suffix="p2", position="2")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{last}/move",
        data={"direction": "down"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "saved=order" in response.headers["location"]
    with world.database.unit_of_work() as uow:
        positions = {t: uow.tasks.get_task(TaskId(t)).position for t in (first, last)}
    assert positions == {first: 1, last: 2}


def test_a_regrade_with_nothing_on_an_older_version_queues_nothing(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/regrade", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tasks/{task_id}/edit?saved=regraded#saved")
    assert world.console.last_regrade == (str(world.course.id), 0)


def test_a_task_with_submissions_is_not_deleted(world: World) -> None:
    """提出のある課題を消すと、その成績が何の課題の点なのか辿れなくなる。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")
    _submit(world, learner, task_id)

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/delete", follow_redirects=False
    )

    assert response.status_code == 409
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)) is not None


def test_a_task_without_submissions_is_deleted(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")

    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/delete", follow_redirects=False
    )

    assert response.status_code == 303
    assert "saved=task_deleted" in response.headers["location"]
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)) is None


def test_clearing_a_unit_deletes_unused_tasks_and_withdraws_used_ones(world: World) -> None:
    """**削除と取り下げを 1 操作で振り分ける**（#59）。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    unused = _add_task(world, client, unit="ex09", suffix="p1")
    used = _add_task(world, client, unit="ex09", suffix="p2")
    _submit(world, learner, used)

    response = client.post(
        f"/manage/courses/{world.course.id}/units/ex09/clear", follow_redirects=False
    )

    assert response.status_code == 303
    assert "saved=unit_cleared" in response.headers["location"]
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(unused)) is None
        assert uow.tasks.get_task(TaskId(used)).withdrawn
    report = world.console.last_clear[1]
    assert len(report.deleted) == 1 and len(report.withdrawn) == 1


def test_clearing_a_unit_with_no_submissions_returns_to_the_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(world, client, unit="ex09", suffix="p1")

    response = client.post(
        f"/manage/courses/{world.course.id}/units/ex09/clear", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/courses/{world.course.id}"


def test_failed_jobs_are_put_back_in_the_queue(world: World) -> None:
    """**教員が押したときだけ動く**（#80）。上限まで落ちたジョブを流し直す。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    client = world.client("teacher")
    task_id = _add_task(world, client, unit="ex01", suffix="p1")
    accepted = _submit(world, learner, task_id)
    now = datetime.now(UTC)
    with world.database.unit_of_work() as uow:
        job = uow.jobs.reserve(now, worker="t", lease_seconds=60, phase=GradingPhase.DETERMINISTIC)
        assert job is not None and job.submission_id == accepted.submission.id
        uow.jobs.update(job.failed(now, "boom", permanent=True))
        uow.commit()
        assert uow.jobs.failed_for([accepted.submission.id])

    response = client.post(
        f"/manage/courses/{world.course.id}/units/ex01/retry-failed", follow_redirects=False
    )

    assert response.status_code == 303
    assert "saved=retried" in response.headers["location"]
    assert world.console.last_release == (str(world.course.id), 1)
    with world.database.unit_of_work() as uow:
        assert not uow.jobs.failed_for([accepted.submission.id])


# --------------------------------------------------------------------------
# 雛形のダウンロード ── 参照 1 回
# --------------------------------------------------------------------------


def test_the_bundle_template_is_a_zip_with_task_definitions(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    _add_task(world, client, unit="ex01", suffix="p1")

    response = client.get(f"/manage/courses/{world.course.id}/units/ex01/bundle/template")

    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert any(name.endswith("task.yaml") for name in names)


def test_the_course_template_is_yaml(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").get("/manage/course-template.yaml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-yaml")
    assert response.text.strip()
