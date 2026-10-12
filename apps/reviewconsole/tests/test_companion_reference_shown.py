"""クライアント・サーバの課題でも、参照解答を課題の編集画面に出す（2026-10-12）。

参照解答は入出力の観点の中にしか出していなかったので、`network_test_runner` の
課題では版が持っているのに画面のどこにも出ず、「参照解答が無い」と読まれた
（network test5/echoClient5）。固定したいのは 3 つ。

見える       観点の見出しに「参照解答あり」、開くと本文
無ければ     「参照解答なし」と出す（あるように見せない）
読むだけ     画面の値をそのまま送っても、参照解答は消えない
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from test_manage import World, _user_id
from test_network_comparison import RUNNER, _payload
from test_task_page_one_save import _as_shown, _latest, _revise

from aijudge_authoring import TaskSpec
from aijudge_authoring import spec as authoring_spec
from aijudge_core import Role
from aijudge_course_admin.authoring import save_task

REFERENCE = "import socket\n\nprint('ask_port reference')\n"


def _world() -> World:
    world = World(Path(tempfile.mkdtemp()))
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(world.course.id)
        uow.identity.save_course(course.model_copy(update={"subject_profile": "cs_network_python"}))
        uow.commit()
    world.register("teacher", Role.INSTRUCTOR)
    return world


def _task(world: World, reference: str | None):
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="test5/p1",
            unit="test5",
            statement="## [必須] エコー ##\n\nサーバに接続する。",
            criteria=(
                authoring_spec.CriterionSpec(
                    code="network",
                    title="通信できる",
                    description="サーバとやり取りできるか。",
                    weight=1.0,
                    evaluator=RUNNER,
                    levels=(
                        authoring_spec.LevelSpec(
                            level=0, label="未達", descriptor="通らない", score_ratio=0.0
                        ),
                        authoring_spec.LevelSpec(
                            level=1, label="達成", descriptor="通る", score_ratio=1.0
                        ),
                    ),
                ),
            ),
            test_cases=(
                authoring_spec.TestCaseSpec(name="connect", evaluator=RUNNER, payload=_payload()),
            ),
            reference_solution=reference,
        ),
        subject_profile="cs_network_python",
        authored_by=_user_id(world, "teacher"),
    )
    return saved.task.id


def _page(world: World, task_id) -> str:
    url = f"/manage/courses/{world.course.id}/tasks/{task_id}/edit"
    return world.client("teacher").get(url).text


def test_the_reference_of_a_client_server_task_is_shown() -> None:
    world = _world()
    try:
        page = _page(world, _task(world, REFERENCE))
        assert "クライアント・サーバ 1 件・参照解答あり" in page
        assert "print(&#39;ask_port reference&#39;)" in page
    finally:
        world.close()


def test_a_missing_reference_is_said_to_be_missing() -> None:
    world = _world()
    try:
        page = _page(world, _task(world, None))
        assert "参照解答なし" in page
        assert "参照解答あり" not in page
    finally:
        world.close()


def test_saving_what_is_shown_keeps_the_reference() -> None:
    world = _world()
    try:
        task_id = _task(world, REFERENCE)
        form = _as_shown(world, task_id)
        form["statement"] = ["## [必須] エコー ##\n\nサーバに接続して送る。"]

        response = _revise(world, task_id, form)

        assert response.status_code == 303, response.text
        after = _latest(world, task_id)
        assert after.version == 2
        assert after.reference_solution == REFERENCE
    finally:
        world.close()
