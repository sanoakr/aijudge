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
    from aijudge_course_admin import groups as audience

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


# -- 科目プロファイルの改名（段階 4-3 の前に厚くする・地図の表）------------------


def _profiles_with_unused(world: World, tmp_path) -> object:
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    (profiles / "cs_unused.yaml").write_text(
        "# なぜこの値なのかの記録\nname: cs_unused\ndeterministic: []\n", encoding="utf-8"
    )
    # コースが参照しているもの（参照中は改名できない）。
    (profiles / f"{world.course.subject_profile}.yaml").write_text(
        f"name: {world.course.subject_profile}\ndeterministic: []\n", encoding="utf-8"
    )
    world.console.profiles_dir = profiles
    return profiles


def test_renaming_a_profile_redirects_to_it_and_records_the_change(world: World, tmp_path) -> None:
    profiles = _profiles_with_unused(world, tmp_path)
    world.register("boss", Role.ADMIN)

    response = world.client("boss").post(
        "/manage/subjects/cs_unused/rename",
        data={"new_name": " cs_renamed "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(
        "/manage/subjects/cs_renamed?saved=profile_renamed#saved"
    )
    # コメントも残る（改名は中身を書き直さない）。
    assert "# なぜこの値なのかの記録" in (profiles / "cs_renamed.yaml").read_text(encoding="utf-8")
    with world.database.unit_of_work() as uow:
        rows = uow.audit.list_for_target("subject_profile", "cs_renamed")
    assert rows and rows[-1].detail.get("renamed_from") == "cs_unused"


def test_a_profile_a_course_uses_is_not_renamed(world: World, tmp_path) -> None:
    """**参照中は改名しない。** 名前で引いているコースの採点が止まる（#146）。"""
    profiles = _profiles_with_unused(world, tmp_path)
    world.register("boss", Role.ADMIN)
    used = world.course.subject_profile

    response = world.client("boss").post(
        f"/manage/subjects/{used}/rename", data={"new_name": "cs_elsewhere"}
    )

    assert response.status_code == 400
    assert (profiles / f"{used}.yaml").exists()
    assert not (profiles / "cs_elsewhere.yaml").exists()


def test_a_profile_is_not_renamed_onto_an_existing_one(world: World, tmp_path) -> None:
    profiles = _profiles_with_unused(world, tmp_path)
    world.register("boss", Role.ADMIN)
    taken = world.course.subject_profile

    response = world.client("boss").post(
        "/manage/subjects/cs_unused/rename", data={"new_name": taken}
    )

    assert response.status_code == 400
    assert (profiles / "cs_unused.yaml").exists()
    assert (profiles / f"{taken}.yaml").read_text(encoding="utf-8").startswith(f"name: {taken}")


def test_only_a_tenant_admin_renames_a_profile(world: World, tmp_path) -> None:
    profiles = _profiles_with_unused(world, tmp_path)
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").post(
        "/manage/subjects/cs_unused/rename", data={"new_name": "cs_renamed"}
    )

    assert response.status_code == 403
    assert (profiles / "cs_unused.yaml").exists()


# -- KC の名前・説明の修正（段階 4-5 の前に厚くする・地図の表）--------------------


def _kc_world(world: World) -> str:
    from test_manage import _seed, _use_kc

    _seed(world)
    world.register("boss", Role.ADMIN)
    world.register("teacher", Role.INSTRUCTOR)
    world.register("ta", Role.ASSISTANT)
    _use_kc(world, "cs.loops.control.basic", "ルーブ")
    return f"/manage/courses/{world.course.id}/kc/edit"


def test_editing_a_kc_trims_the_key_and_returns_to_the_kc_page(world: World) -> None:
    url = _kc_world(world)
    response = world.client("teacher").post(
        url, data={"key": " cs.loops.control.basic ", "label": "ループ"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(
        f"/manage/courses/{world.course.id}/kc?saved=kc_edited#saved"
    )


def test_a_ta_does_not_edit_a_kc(world: World) -> None:
    url = _kc_world(world)
    response = world.client("ta").post(url, data={"key": "cs.loops.control.basic", "label": "x"})
    assert response.status_code == 403


def test_an_unknown_kc_or_an_empty_label_is_refused(world: World) -> None:
    url = _kc_world(world)
    client = world.client("teacher")
    unknown = client.post(url, data={"key": "cs.no.such.kc", "label": "x"})
    assert unknown.status_code == 400
    empty = client.post(url, data={"key": "cs.loops.control.basic", "label": ""})
    assert empty.status_code == 400
    # 断ったあとも名前は元のまま。
    page = client.get(f"/manage/courses/{world.course.id}/kc").text
    assert "ルーブ" in page


# -- 問題セットを開く・補完の切り替え（段階 4-7 の前に厚くする・地図の表）----------


def test_opening_a_unit_saves_nothing_and_encodes_the_name(world: World) -> None:
    """**保存は伴わない。** 回は課題の属性で、最初の 1 問を足した時点で実在する。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    named = client.post(
        f"/manage/courses/{world.course.id}/units", data={"unit": " 第3回 "}, follow_redirects=False
    )
    assert named.status_code == 303
    assert named.headers["location"].endswith(
        f"/manage/courses/{world.course.id}/units/%E7%AC%AC3%E5%9B%9E"
    )
    # 名前が空なら「未分類」（`_`）を開く。
    empty = client.post(
        f"/manage/courses/{world.course.id}/units", data={"unit": ""}, follow_redirects=False
    )
    assert empty.headers["location"].endswith(f"/manage/courses/{world.course.id}/units/_")
    with world.database.unit_of_work() as uow:
        assert not uow.tasks.list_for_course(world.course.id)


def test_a_ta_does_not_open_a_unit(world: World) -> None:
    world.register("ta", Role.ASSISTANT)
    response = world.client("ta").post(
        f"/manage/courses/{world.course.id}/units", data={"unit": "ex09"}
    )
    assert response.status_code == 403


def test_completion_is_switched_for_the_whole_unit(world: World) -> None:
    """**補完はセット単位。** 全課題に入り、空の値は「出さない」。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    first = _add_task(world, client, unit="ex07", suffix="p1")
    second = _add_task(world, client, unit="ex07", suffix="p2")
    url = f"/manage/courses/{world.course.id}/units/ex07/completion"

    on = client.post(url, data={"completion": "1"}, follow_redirects=False)
    assert on.status_code == 303
    assert "saved=completion" in on.headers["location"]
    with world.database.unit_of_work() as uow:
        assert all(uow.tasks.get_task(TaskId(t)).editor_completion for t in (first, second))

    client.post(url, data={"completion": ""})
    with world.database.unit_of_work() as uow:
        assert not any(uow.tasks.get_task(TaskId(t)).editor_completion for t in (first, second))
