"""遅延の減点ルールを共通設定の画面で入れる（ADR 0013・2026-10-01）。

以前は画面が無く、DB を直接書き換えて入れていた（監査に残らなかった）。
固定したいのは 5 つ。

% で入れて割合で残す    「50」は 0.5 として保存され、画面には 50 と出る
開いて送れば変化なし    減点ルールのあるコースでも、そのまま送れば何も書かない
空にすれば消える        すべての行を空にすると減点しない
おかしな値は断る        片方だけの行・同じ時間の段・範囲外の % は保存しない
監査に前後を残す        減点は採点時に焼き付くので、いつから何が効いたかを追える
"""

from __future__ import annotations

import pytest
from test_manage import TENANT, World
from test_manage import world as world  # フィクスチャを借りる
from test_manage_course_settings_save import _as_shown, _course, _settings

from aijudge_audit import AuditAction
from aijudge_core import LatePenaltyStep, Role
from aijudge_core.ids import TenantId
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.late_penalty import parse_steps


def _with_rows(
    form: dict[str, list[str]], rows: list[tuple[str, str]], first: str = ""
) -> dict[str, list[str]]:
    """1 行目（締切を過ぎたときの減点率）と、2 行目以降（遅延時間と減点率）を入れる。"""
    form["penalty_first_percent"] = [first]
    form["penalty_hours"] = [hours for hours, _ in rows]
    form["penalty_percent"] = [percent for _, percent in rows]
    return form


def test_percent_is_stored_as_a_ratio_and_shown_as_percent(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    form = _with_rows(_as_shown(world, client), [("24", "30"), ("", "")], first="10")

    response = client.post(_settings(world), data=form, follow_redirects=False)

    assert "saved=course_settings" in response.headers["location"]
    assert _course(world).late_penalty_steps == (
        LatePenaltyStep(after_hours=0, ratio=0.1),
        LatePenaltyStep(after_hours=24, ratio=0.3),
    ), "時間の昇順に並べ、% を割合にする"
    shown = _as_shown(world, client)
    assert shown["penalty_first_percent"] == ["10"], "1 行目は締切後の減点率だけ"
    assert shown["penalty_hours"][0] == "24" and shown["penalty_percent"][0] == "30"


def test_saving_what_is_shown_keeps_the_rule(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("0", "50")]))

    response = client.post(_settings(world), data=_as_shown(world, client), follow_redirects=False)

    assert "saved=unchanged" in response.headers["location"]
    assert _course(world).late_penalty_steps == (LatePenaltyStep(after_hours=0, ratio=0.5),)


def test_clearing_every_row_removes_the_rule(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("0", "50")]))

    form = _with_rows(_as_shown(world, client), [("", ""), ("", ""), ("", "")])
    client.post(_settings(world), data=form)

    assert _course(world).late_penalty_steps == ()


def test_a_form_without_the_rows_does_not_clear_the_rule(world: World) -> None:
    """**欄の無い古い画面から保存しても消さない。** 印の無い送信は減点ルールに触らない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("0", "50")]))

    form = _as_shown(world, client)
    for name in ("penalty_present", "penalty_hours", "penalty_percent"):
        form.pop(name)
    form["after_minutes"] = ["90"]
    client.post(_settings(world), data=form)

    assert _course(world).late_penalty_steps == (LatePenaltyStep(after_hours=0, ratio=0.5),)


@pytest.mark.parametrize(
    "rows",
    [
        [("24", "")],
        [("0", "10"), ("0", "20")],
        [("0", "0")],
        [("0", "150")],
        [("-1", "10")],
        [("約1日", "10")],
    ],
)
def test_a_malformed_rule_is_refused_and_nothing_is_written(world: World, rows) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    form = _with_rows(_as_shown(world, client), rows)
    form["after_minutes"] = ["90"]

    response = client.post(_settings(world), data=form, follow_redirects=False)

    assert response.status_code == 400
    after = _course(world)
    assert after.late_penalty_steps == ()
    assert after.auto_finalize_after_minutes is None, "1 つ断られたら何も書かない"


def test_the_change_is_audited_with_both_values_in_percent(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("0", "50")]))

    with world.database.unit_of_work() as uow:
        events = uow.audit.list_recent(TenantId(TENANT), action=AuditAction.COURSE_UPDATED)
    penalty = [event for event in events if "late_penalty_steps" in event.detail]
    assert len(penalty) == 1
    assert penalty[0].detail["late_penalty_steps"] == {
        "before": [],
        "after": [{"after_hours": 0.0, "percent": 50.0}],
    }


def test_the_parser_reads_fractional_values() -> None:
    assert parse_steps([("1.5", "12.5")]) == (LatePenaltyStep(after_hours=1.5, ratio=0.125),)
    with pytest.raises(AdminError):
        parse_steps([("", "10")])


def test_the_first_row_takes_only_the_rate_after_the_deadline(world: World) -> None:
    """**1 行目は締切後の減点率だけ。** 遅延時間は 2 行目以降で入れる（2026-10-08）。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("", "")], first="50"))

    assert _course(world).late_penalty_steps == (LatePenaltyStep(after_hours=0, ratio=0.5),)
    page = client.get(f"/manage/courses/{world.course.id}").text
    assert 'name="penalty_first_percent"' in page
    assert page.count('name="penalty_hours"') >= 2, "2 行目以降に遅延時間の欄が無い"


def test_a_rule_without_a_zero_hour_step_survives_a_resave(world: World) -> None:
    """24 時間を超えてから減点するルールは、1 行目が空のまま保存し直しても変わらない。"""
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")
    client.post(_settings(world), data=_with_rows(_as_shown(world, client), [("24", "30")]))
    assert _course(world).late_penalty_steps == (LatePenaltyStep(after_hours=24, ratio=0.3),)

    shown = _as_shown(world, client)
    assert shown["penalty_first_percent"] == [""], "0 時間の段を勝手に作っている"
    response = client.post(_settings(world), data=shown, follow_redirects=False)

    assert "saved=unchanged" in response.headers["location"]
    assert _course(world).late_penalty_steps == (LatePenaltyStep(after_hours=24, ratio=0.3),)


def test_the_course_page_lists_units_that_have_their_own_rule(world: World) -> None:
    """コースの設定を変えても動かない回を、変える前に見せる。"""
    world.register("teacher", Role.INSTRUCTOR)
    from test_manage import _import_example, _unit_of

    task_id = _import_example(world)
    unit = _unit_of(world)
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(task_id)
        uow.tasks.save_task(
            task.model_copy(
                update={"late_penalty_steps": (LatePenaltyStep(after_hours=0, ratio=0.5),)}
            )
        )
        uow.commit()

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert "自分の設定" in page and f"/units/{unit}" in page
