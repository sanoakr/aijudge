"""blind 採点の訂正を**誰が**できるか（ADR 0031・#706）。

規則は「採点した本人か、担当教員」。`test_blind_correction.py` の成功側は
blind 採点をした教員本人が直すので、本人の判定で短絡して教員の判定に届かず、
拒否側は TA が拒まれることしか見ていなかった。そのため条件を「本人だけ」にも
「教員だけ」にも書き換えられた。ここでは TA が付けた blind 採点を用意して、
3 つの立場を分けて確かめる。

本人の TA      … 直せる（押し間違いは本人がいちばんよく分かる）
別の担当教員    … 直せる（TA の採点は教員も直せる）
別の TA        … 403
"""

from __future__ import annotations

from test_blind_correction import REASON, _levels_form, _machine
from test_console import World, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる

from aijudge_core import Role


def _marked_by_an_assistant(world: World):
    """TA の `ta` が、AI と 1 観点だけ食い違う blind 採点を付けた提出。"""
    learner = world.register("s2400001", role=Role.LEARNER)
    world.register("ta", role=Role.ASSISTANT)
    accepted = world.submit(learner)
    world.worker.run_until_empty()
    machine = _machine(world, accepted.submission.id)
    criterion = next(iter(machine))
    wrong = machine | {criterion: 0 if machine[criterion] else 1}
    world.login("ta")
    marked = world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data=_levels_form(world, wrong),
        follow_redirects=False,
    )
    assert marked.status_code == 303, marked.text
    return accepted.submission.id, machine


def _correct(world: World, submission_id, levels: dict):
    return world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(world, levels) | {"reason": REASON},
        follow_redirects=False,
    )


def _corrections(world: World, submission_id):
    with world.database.unit_of_work() as uow:
        return uow.reviews.blind_corrections(submission_id)


@needs_c_compiler
def test_the_assistant_who_marked_may_correct_their_own_mark(blind_world: World) -> None:
    """**TA 本人は直せる。** 条件を「担当教員だけ」に狭めると、ここで 403 になる。"""
    submission_id, machine = _marked_by_an_assistant(blind_world)

    response = _correct(blind_world, submission_id, machine)

    assert response.status_code == 303, response.text
    (correction,) = _corrections(blind_world, submission_id)
    assert correction.levels == machine


@needs_c_compiler
def test_an_instructor_who_did_not_mark_may_correct_an_assistants_mark(
    blind_world: World,
) -> None:
    """**採点した本人でない担当教員も直せる。** 条件を「本人だけ」に狭めると 403 になる。"""
    submission_id, machine = _marked_by_an_assistant(blind_world)
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    blind_world.login("instructor")

    response = _correct(blind_world, submission_id, machine)

    assert response.status_code == 303, response.text
    (correction,) = _corrections(blind_world, submission_id)
    assert correction.levels == machine


@needs_c_compiler
def test_another_assistant_may_not_correct_the_mark(blind_world: World) -> None:
    """本人でも担当教員でもない TA は 403。訂正は記録されない。"""
    submission_id, machine = _marked_by_an_assistant(blind_world)
    blind_world.register("ta2", role=Role.ASSISTANT)
    blind_world.login("ta2")

    response = _correct(blind_world, submission_id, machine)

    assert response.status_code == 403
    assert _corrections(blind_world, submission_id) == ()
