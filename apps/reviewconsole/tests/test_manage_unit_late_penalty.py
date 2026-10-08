"""遅延の減点を問題セット単位で上書きする（ADR 0013・2026-10-08）。

減点の段はコースに 1 つしか持てず、prog2 の「3 回目以降だけ 50% 減」が表せなかった。
固定したいのは 6 つ。

回だけ決められる      「この回だけ決める」で入れた段は、その回の全課題に入る
コースに戻せる        「コースの設定に従う」にすれば None（コースの段が当たる）
空欄は減点しない      「この回だけ決める」で行をすべて空にすると、コースに段があっても減点しない
触らなければ書かない  印の無い古い画面や、開いてそのまま送った保存は減点に触らない
おかしな値は断る      片方だけの行は保存しない（何も書かれない）
監査に % で残す       減点は採点時に焼き付くので、いつから何が効いたかを追える
"""

from __future__ import annotations

from test_manage import World, _import_example, _unit_of
from test_manage import world as world  # フィクスチャを借りる

from aijudge_audit import AuditAction
from aijudge_core import LatePenaltyStep, Role, Task
from aijudge_core.ids import TaskId, TenantId

TENANT = "ten_" + "0" * 32
HALF = (LatePenaltyStep(after_hours=0.0, ratio=0.5),)


def _task(world: World, task_id: str) -> Task:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
    assert task is not None
    return task


def _settings(world: World, unit: str) -> str:
    return f"/manage/courses/{world.course.id}/units/{unit}/settings"


def _form(**overrides: object) -> dict[str, object]:
    """画面を開いてそのまま送ったときの値（例題の問題セットの既定）。"""
    return {"session": "", "file_upload": "1"} | overrides


def _penalty(mode: str, rows: list[tuple[str, str]], first: str = "") -> dict[str, object]:
    """`first` は 1 行目（締切を過ぎたときの減点率）、`rows` は 2 行目以降。"""
    return {
        "penalty_present": "1",
        "penalty_mode": mode,
        "penalty_first_percent": first,
        "penalty_hours": [hours for hours, _ in rows],
        "penalty_percent": [percent for _, percent in rows],
    }


def _save(world: World, unit: str, **extra: object):
    return world.client("teacher").post(
        _settings(world, unit), data=_form(**extra), follow_redirects=False
    )


def test_the_page_offers_the_choice_and_defaults_to_the_course(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}/units/{unit}").text

    assert 'name="penalty_mode"' in page
    assert "コースの設定に従う" in page and "この回だけ決める" in page
    assert page.count('name="penalty_hours"') >= 2, "段を足せる空の行が出ていない"
    assert 'name="penalty_first_percent"' in page, "1 行目（締切後の減点率）が無い"
    assert "いま効いている減点" in page


def test_a_rule_entered_for_the_unit_reaches_every_task_in_it(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = _save(world, unit, **_penalty("unit", [("", "")], first="50"))

    assert response.status_code == 303 and "saved=settings" in response.headers["location"]
    assert _task(world, task_id).late_penalty_steps == HALF


def test_following_the_course_clears_the_unit_rule(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    _save(world, unit, **_penalty("unit", [], first="50"))

    _save(world, unit, **_penalty("course", [], first="50"))

    assert _task(world, task_id).late_penalty_steps is None


def test_blank_rows_mean_no_penalty_for_this_unit(world: World) -> None:
    """コースに段があっても、この回だけ外せる。None（従う）とは別の値になる。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    _save(world, unit, **_penalty("unit", [("", ""), ("", "")]))

    assert _task(world, task_id).late_penalty_steps == ()


def test_a_form_without_the_mark_does_not_touch_the_rule(world: World) -> None:
    """**欄の無い古い画面から保存しても消さない。**"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)
    _save(world, unit, **_penalty("unit", [], first="50"))

    response = _save(world, unit, session="4")

    assert response.status_code == 303
    assert _task(world, task_id).late_penalty_steps == HALF


def test_saving_what_is_shown_changes_nothing(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    _save(world, unit, **_penalty("unit", [], first="50"))

    response = _save(world, unit, **_penalty("unit", [("", "")], first="50"))

    assert "saved=unchanged" in response.headers["location"]


def test_a_half_filled_row_is_refused_and_nothing_is_written(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _import_example(world)
    unit = _unit_of(world)

    response = _save(world, unit, session="7", **_penalty("unit", [("12", "")]))

    assert response.status_code == 400
    after = _task(world, task_id)
    assert after.late_penalty_steps is None
    assert after.session != 7, "同時に送った回番号が書かれている"


def test_the_change_is_audited_in_percent(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    _save(world, unit, **_penalty("unit", [], first="50"))

    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(TenantId(TENANT), action=AuditAction.TASK_UPDATED)
    changed = [e for e in events if "late_penalty_steps" in e.detail.get("changed", {})]
    assert len(changed) == 1
    assert changed[0].detail["changed"]["late_penalty_steps"] == {
        "before": None,
        "after": [{"after_hours": 0.0, "percent": 50.0}],
    }


def test_the_page_shows_which_rule_is_in_force(world: World) -> None:
    """ユニットが従っているのがコースか、この回の設定かを、画面で読み分けられる。"""
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)
    unit = _unit_of(world)
    page_url = f"/manage/courses/{world.course.id}/units/{unit}"
    assert "（コースの設定）" in world.client("teacher").get(page_url).text

    _save(world, unit, **_penalty("unit", [], first="50"))

    page = world.client("teacher").get(page_url).text
    assert "（この回の設定）" in page
    assert "総合点から 50% を引く" in page
