"""どの観点も使っていない検証データを、保存のときに外す（2026-09-28）。

観点の評価器を `code_test_runner` から AI に変えると、入出力と参照解答は画面から
見えなくなるのに版に残り、提出のたびに実行され続けた（どの観点の点にもならない）。
保存のときに外し、**外したことと、前の版に残っていることを言う。**
"""

from __future__ import annotations

from test_manage import World, _task_with_tests
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskId

STATEMENT = "## [必須] 合計 ##\n\n合計を出力する。"


def _versions(world: World, task_id: str):
    with world.database.unit_of_work() as uow:
        return uow.tasks.list_versions(TaskId(task_id))


def _switch_to_ai(world: World, task_id: str):
    client = world.client("teacher")
    return client, client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={
            "statement": STATEMENT,
            "criterion_code": ["correctness"],
            "criterion_title": ["出力の正しさ"],
            "criterion_description": ["仕様どおりか。"],
            "criterion_weight": ["1.0"],
            # 空欄は AI（rubric_ai_judge）
            "criterion_evaluator": [""],
            "criterion_levels": [""],
        },
        follow_redirects=False,
    )


def test_switching_to_ai_drops_the_tests_and_the_reference(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    before = _versions(world, task_id)[0]
    assert len(before.test_cases) == 3 and before.reference_solution

    _, response = _switch_to_ai(world, task_id)
    assert response.status_code == 303, response.text

    latest, previous = _versions(world, task_id)[:2]
    assert latest.version == before.version + 1
    assert latest.test_cases == ()
    assert latest.reference_solution is None
    # 版は書き換えない ── 外したものは前の版に残る（「版の復元」で戻せる）
    assert len(previous.test_cases) == 3
    assert previous.reference_solution == before.reference_solution


def test_the_page_says_what_was_dropped_once(world: World) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client, response = _switch_to_ai(world, task_id)

    page = client.get(response.headers["location"]).text
    assert "どの観点も使っていないテストケース 3 件と参照解答を外しました" in page
    assert "v1 に残っています" in page
    # 一度だけ
    assert "外しました" not in client.get(response.headers["location"]).text


def test_data_that_a_criterion_still_reads_is_kept(world: World) -> None:
    """問題文だけを直す訂正では、テストを読む観点が残るので何も外さない。"""
    world.register("teacher", Role.INSTRUCTOR)
    task_id = _task_with_tests(world)
    client = world.client("teacher")
    response = client.post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/revise",
        data={"statement": STATEMENT + "\n\n誤字を直しました。"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    latest = _versions(world, task_id)[0]
    assert len(latest.test_cases) == 3
    assert latest.reference_solution is not None
    assert "外しました" not in client.get(response.headers["location"]).text
