"""確認後に返す回は、教員だけが結果を返せる（ADR 0033 §6）。

TA は断られる    1 件目の確定でも断る（画面のボタンも押せない）
教員は返せる     同じ提出を教員は確定できる
ほかの回は従来   設定の無い回は、TA も 1 件目を確定できる
blind の一致     TA の blind が AI と一致しても、その場で確定しない
"""

from __future__ import annotations

from test_console import World, _agree_form, _instructor_and_submission, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role


def _hold(world: World) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update={"results_after_review": True}))
        uow.commit()


def _graded(world: World):
    instructor, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    return instructor, accepted.submission.id, run, machine


def _reviews(world: World, run) -> int:
    with world.database.unit_of_work() as uow:
        return len(uow.reviews.reviews_for_run(run.id))


@needs_c_compiler
def test_an_assistant_cannot_return_the_result(world: World) -> None:
    _hold(world)
    _instructor, submission_id, run, machine = _graded(world)
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")

    page = world.client.get(f"/review/{submission_id}/reveal").text
    response = world.client.post(
        f"/review/{submission_id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )

    assert "結果を学習者に返せるのは担当教員だけ" in page
    assert response.status_code == 403
    assert _reviews(world, run) == 0


@needs_c_compiler
def test_an_instructor_can_return_it(world: World) -> None:
    _hold(world)
    _instructor, submission_id, run, machine = _graded(world)

    response = world.client.post(
        f"/review/{submission_id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert _reviews(world, run) == 1


@needs_c_compiler
def test_an_assistant_can_still_finalize_other_units(world: World) -> None:
    _instructor, submission_id, run, machine = _graded(world)
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")

    response = world.client.post(
        f"/review/{submission_id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert _reviews(world, run) == 1


@needs_c_compiler
def test_an_assistants_agreeing_blind_mark_does_not_return_it(blind_world: World) -> None:
    _hold(blind_world)
    _instructor, submission_id, run, machine = _graded(blind_world)
    blind_world.register("ta", role=Role.ASSISTANT)
    blind_world.login("ta")

    response = blind_world.client.post(
        f"/review/{submission_id}/blind",
        data=_agree_form(blind_world, machine),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert _reviews(blind_world, run) == 0


@needs_c_compiler
def test_an_instructors_agreeing_blind_mark_returns_it(blind_world: World) -> None:
    _hold(blind_world)
    _instructor, submission_id, run, machine = _graded(blind_world)

    response = blind_world.client.post(
        f"/review/{submission_id}/blind",
        data=_agree_form(blind_world, machine),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert _reviews(blind_world, run) == 1
