"""問題のページの「この問題の設定」（日程・提出形式・実行時間の上限・2026-09-27）。

操作のタブを詰めたとき、版を上げない 3 つの値を 1 枚のフォームにした。
固定したいのは 4 つ。

まとめて保存   3 つが 1 回の保存で入り、版は上がらない。
監査は種類ごと 日程・提出形式・上限の記録は今までと同じ文言で、変わったものだけ。
空は断る       提出形式を 1 つも選ばない保存は断る（何も提出できなくなる）。
内容は消さない 内容のフォームは提出形式を送らなくなった。問題文を直しても、
               教員が選んだ形式はそのまま（コースの既定に戻らない）。
"""

from __future__ import annotations

from test_manage import World, _task_with_tests
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId


def _url(world: World, task_id: str, tail: str) -> str:
    return f"/manage/courses/{world.course.id}/tasks/{task_id}/{tail}"


def _settings(**extra) -> dict:
    form = {
        "opens_at": "2026-10-01T10:40",
        "submissions_open_at": "",
        "due_at": "2026-10-08T23:59",
        "accepts_until": "",
        "grading_starts_at": "",
        "formats": "1",
        "suffix": [".c", ".pdf"],
        "case_timeout_seconds": "",
    }
    form.update(extra)
    return form


def test_the_three_settings_are_one_save_without_a_new_version(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")

    page = client.get(_url(world, task_id, "edit")).text
    settings = page[page.index('id="task-settings"') :]
    settings = settings[: settings.index("</form>")]
    for name in ('name="due_at"', 'name="suffix"', 'name="case_timeout_seconds"'):
        assert name in settings, f"{name} が設定のフォームに無い"
    assert settings.count('type="submit"') == 1, "保存は 1 つ"

    response = client.post(
        _url(world, task_id, "settings"),
        data=_settings(case_timeout_seconds="30"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("saved=task_settings#saved")

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        version = uow.tasks.latest_version(TaskId(task_id))
        audit = [row.summary for row in uow.audit.list_for_target("task", task_id)]
    assert task.due_at is not None
    assert task.accepted_suffixes == (".c", ".pdf")
    assert task.case_timeout_seconds == 30.0
    assert version.version == 1, "設定を保存しただけで版が上がっている"
    assert sorted(audit) == sorted(
        ["課題の日程を変えた", "課題の提出形式を変えた", "課題の実行時間の上限を変えた"]
    )


def test_only_what_changed_is_recorded(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")
    client.post(_url(world, task_id, "settings"), data=_settings())

    # 締切だけ動かす。
    client.post(_url(world, task_id, "settings"), data=_settings(due_at="2026-10-09T23:59"))

    with world.database.unit_of_work() as uow:
        rows = uow.audit.list_for_target("task", task_id)
    summaries = [row.summary for row in rows]
    assert summaries.count("課題の日程を変えた") == 2
    assert summaries.count("課題の提出形式を変えた") == 1


def test_choosing_no_format_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    with world.database.unit_of_work() as uow:
        before = uow.tasks.get_task(TaskId(task_id)).accepted_suffixes

    response = world.client("teacher").post(
        _url(world, task_id, "settings"), data=_settings(suffix=[]), follow_redirects=False
    )

    assert response.status_code == 400
    assert "1 つ以上" in response.text
    with world.database.unit_of_work() as uow:
        assert uow.tasks.get_task(TaskId(task_id)).accepted_suffixes == before


def test_a_limit_past_the_ceiling_is_refused(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)

    response = world.client("teacher").post(
        _url(world, task_id, "settings"),
        data=_settings(case_timeout_seconds="120"),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "60 秒以下" in response.text


def test_saving_the_content_keeps_the_chosen_formats(world: World) -> None:
    """内容のフォームは提出形式を送らない。問題文を直しても形式は消えない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")
    client.post(_url(world, task_id, "settings"), data=_settings(suffix=[".pdf"]))

    page = client.get(_url(world, task_id, "edit")).text
    content = page[: page.index('id="tab-ops"')]
    assert 'name="suffix"' not in content, "内容のフォームが提出形式をまだ送っている"

    with world.database.unit_of_work() as uow:
        version = uow.tasks.latest_version(TaskId(task_id))
    response = client.post(
        _url(world, task_id, "revise"),
        data={"statement": version.statement + "\n\n（誤字を直した）", "readability_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
        latest = uow.tasks.latest_version(TaskId(task_id))
    assert latest.version == version.version + 1
    assert task.accepted_suffixes == (".pdf",), "問題文を直したら提出形式が既定に戻った"


def test_the_drastic_operations_sit_apart_and_quiet(world: World) -> None:
    """取り下げと削除は底の控えめな枠に、副次の見た目のボタンで並ぶ。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)

    page = world.client("teacher").get(_url(world, task_id, "edit")).text
    zone = page[page.index('class="danger-zone"') :]
    zone = zone[: zone.index('id="saved"') if 'id="saved"' in zone else len(zone)]

    assert '<button type="submit" class="minor">出題の取り下げ</button>' in zone
    assert '<button type="submit" class="minor danger">この課題を削除</button>' in zone
    # 確かめの画面は今までどおり出す。
    assert 'data-confirm-title="この課題を削除しますか"' in zone
    assert page.index('id="task-settings"') < page.index('class="danger-zone"')
