"""AI 段階の採点が届く前は、人の確定で成績を閉じない（#400・#706）。

採点は決定的段階 → AI 段階の 2 つのジョブに分かれて流れる（ADR 0011）。
決定的段階だけ終わった提出を確定すると、あとから届いた AI 段階の採点
（新しい `GradingRun`）が確定済みの成績を追い越す。そこで確定の POST は
409 で断り、blind 採点が一致しても自動では確定しない（`_blind_agrees`）。

ほかのテストはどれも `run_until_empty()` で両段階を流してから操作するので、
この分岐を一度も通っていなかった。ここでは決定的段階だけを流し、AI 段階の
ジョブを待ち行列に残した状態で叩く。
"""

from __future__ import annotations

from test_console import World, _agree_form, _instructor_and_submission, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import GradingPhase


def _deterministic_only(world: World):
    """決定的段階だけを流した提出。AI 段階のジョブは残っている。"""
    _instructor, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty(phase=GradingPhase.DETERMINISTIC)
    submission_id = accepted.submission.id
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        assert uow.jobs.awaiting(submission_id, GradingPhase.AI), "AI 段階が残っていない"
    assert run is not None, "決定的段階の採点が無いと、確定の前に別の理由で止まる"
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    return submission_id, run, machine


def _records(world: World, run):
    """この採点に付いた教員の確認と確定（どちらも無いはず）。"""
    with world.database.unit_of_work() as uow:
        return (
            uow.reviews.find_review_for_run(run.id),
            uow.reviews.find_finalization_for_run(run.id),
        )


@needs_c_compiler
def test_finalizing_before_the_ai_phase_arrives_is_refused(world: World) -> None:
    submission_id, run, machine = _deterministic_only(world)

    response = world.client.post(
        f"/review/{submission_id}/finalize",
        data=_agree_form(world, machine) | {"run_id": str(run.id)},
        follow_redirects=False,
    )

    assert response.status_code == 409, response.text
    assert "AI 評価がまだ届いていません" in response.text
    assert _records(world, run) == (None, None)


@needs_c_compiler
def test_finalizing_works_once_the_ai_phase_has_arrived(world: World) -> None:
    """上の 409 が AI 段階の待ちから来ていることの対照。届けば同じ操作で確定できる。"""
    submission_id, _run, _machine = _deterministic_only(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
    machine = {score.criterion_id: score.level for score in run.criterion_scores}

    response = world.client.post(
        f"/review/{submission_id}/finalize",
        data=_agree_form(world, machine) | {"run_id": str(run.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    review, _finalization = _records(world, run)
    assert review is not None


@needs_c_compiler
def test_a_blind_mark_does_not_finalize_while_the_ai_phase_is_pending(
    blind_world: World,
) -> None:
    """blind の段階が手元の採点と一致しても、**確定せずに reveal へ回す。**

    決定的段階だけの採点では AI の観点が未採点（`unscored_criteria`）なので、
    `_blind_agrees` はその条件でも偽になる。`awaiting_ai` の条件はいまは重ねての
    守りで、ここで固定するのは「確認も確定も書かない」という結果の側である。
    """
    submission_id, run, machine = _deterministic_only(blind_world)
    form = {f"level_{c.code}": str(machine.get(c.id, 0)) for c in blind_world.task_version.criteria}

    response = blind_world.client.post(
        f"/review/{submission_id}/blind", data=form, follow_redirects=False
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(f"/review/{submission_id}/reveal")
    assert _records(blind_world, run) == (None, None)
