"""入出力の編集欄の「終了コード」（2026-10-18）。

ケースごとに `0`・`0 以外`・`問わない` を選べる。固定したいのは、**保存で
期待が落ちないこと**である。コンソールの保存は版を作り直すので、欄から
読み落とした値は黙って既定に戻る ── 検証データを壊した前例がある（ex01-2）。

見たまま送れば   画面の値をそのまま送ると何も変わらない（版も上がらない）
選んで保存       `0 以外` を選ぶと payload に入り、門 1 にもその期待で渡る
欄が無ければ     古い画面・API からの送信は、いまの版の期待を名前で引き継ぐ
"""

from __future__ import annotations

import tempfile
import typing
from pathlib import Path

import pytest
from test_manage import World, _case_form, _user_id
from test_task_page_one_save import _as_shown, _latest, _revise

from aijudge_authoring import TaskSpec
from aijudge_authoring.spec import TestCaseSpec
from aijudge_core import Role
from aijudge_core.ids import TaskId
from aijudge_course_admin.authoring import save_task
from aijudge_eval_code_test_runner import EXIT_EXPECTATIONS


@pytest.fixture
def world():
    instance = World(Path(tempfile.mkdtemp()))
    instance.register("teacher", Role.INSTRUCTOR)
    yield instance
    instance.close()


def _task(world: World) -> str:
    saved = save_task(
        world.database,
        course_id=world.course.id,
        spec=TaskSpec(
            key="ex01/p1",
            unit="ex01",
            statement="## [必須] 2 倍 ##\n\n正の整数なら 2 倍を出力する。",
            reference_solution="#include <stdio.h>\nint main(void){return 0;}\n",
            test_cases=(
                TestCaseSpec(name="ok", input="4\n", expected="8\n"),
                TestCaseSpec(name="bad", input="-1\n", expected="", exit_code="nonzero"),
            ),
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_user_id(world, "teacher"),
    )
    return str(saved.task.id)


def _exits(version) -> dict[str, object]:
    return {case.name: case.payload.get("exit_code") for case in version.test_cases}


def _edit(world: World, task_id: str, form):
    return world.client("teacher").post(
        f"/manage/courses/{world.course.id}/tasks/{task_id}/test-cases/edit",
        data=form,
        follow_redirects=False,
    )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> list:
    """門 1 に渡った課題を記録して、常に通す（コンパイラなしで走らせる）。"""
    seen: list = []

    def passes(self, candidate, source):
        seen.append(candidate)
        return True, ""

    monkeypatch.setattr("aijudge_course_admin.task_verifier.TaskVerifier.passes", passes)
    return seen


def test_the_choices_on_screen_are_the_ones_the_evaluator_reads() -> None:
    """画面と宣言と評価器が同じ語彙を使う。1 つだけ増やすと採点で `zero` に倒れる。"""
    declared = typing.get_args(TestCaseSpec.model_fields["exit_code"].annotation)
    assert set(declared) == set(EXIT_EXPECTATIONS)


def test_saving_what_is_shown_keeps_the_expectation(world: World, gate: list) -> None:
    task_id = _task(world)
    before = _latest(world, task_id)
    form = _as_shown(world, task_id)
    assert form["case_exit"][:2] == ["zero", "nonzero"], "画面がいまの期待を選んでいない"

    response = _revise(world, task_id, form)

    assert response.status_code == 303, response.text
    after = _latest(world, task_id)
    assert after.version == before.version, "見たまま送ったら版は上がらない"
    assert _exits(after) == {"ok": None, "bad": "nonzero"}


def test_choosing_nonzero_saves_it_and_the_gate_sees_it(world: World, gate: list) -> None:
    task_id = _task(world)
    before = _latest(world, task_id)
    form = _case_form(before)
    form["case_exit"] = ["any", "nonzero"]

    response = _edit(world, task_id, form)

    assert response.status_code == 303, response.text
    after = _latest(world, task_id)
    assert after.version == before.version + 1
    assert _exits(after) == {"ok": "any", "bad": "nonzero"}
    # **門 1 も同じ期待で走る。** 落とすと、`return 1;` の参照解答が門で断られる。
    assert _exits(gate[-1]) == {"ok": "any", "bad": "nonzero"}


def test_a_form_without_the_column_keeps_the_current_expectation(world: World, gate: list) -> None:
    """古い画面・API からの送信。既定に倒すと、保存のたびに `zero` へ戻る。"""
    task_id = _task(world)
    before = _latest(world, task_id)
    form = _case_form(before, **{"0": {"expected": "8 \n"}})
    assert "case_exit" not in form

    response = _edit(world, task_id, form)

    assert response.status_code == 303, response.text
    assert _exits(_latest(world, task_id)) == {"ok": None, "bad": "nonzero"}


def test_an_unknown_choice_is_refused(world: World, gate: list) -> None:
    task_id = _task(world)
    form = _case_form(_latest(world, task_id))
    form["case_exit"] = ["zero", "1"]

    response = _edit(world, task_id, form)

    assert response.status_code == 400
    assert _exits(_latest(world, TaskId(task_id))) == {"ok": None, "bad": "nonzero"}
