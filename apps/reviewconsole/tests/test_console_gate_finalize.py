"""AND で打ち切った観点を、個別の確定でどう扱うか（#675）。

上位が 0% のまま    打ち切った観点は入力不要。送られた段階は採らず、0% で確定する
上位を引き上げた    打ち切りは消える。下位の段階が無ければ断り、あれば点に入れる
画面               打ち切った観点に必須の入力欄を出さない
"""

from __future__ import annotations

from test_console import COURSE, JUSTIFICATION, World, needs_c_compiler
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Aggregation, Role, final_score

# どのテストケースにも合わない出力（何も出さない）。正しさの観点が 0% になる。
WRONG_PROGRAM = b"int main(void) { return 0; }\n"


def _gated_submission(world: World):
    """上位（正しさ）が 0% で、下位（読みやすさ）が打ち切られた提出。"""
    with world.database.unit_of_work() as uow:
        course = uow.identity.get_course(COURSE)
        assert course is not None
        uow.identity.save_course(course.model_copy(update={"rubric_aggregation": Aggregation.AND}))
        uow.commit()
    learner = world.register("s2400001", role=Role.LEARNER)
    world.register("instructor", role=Role.INSTRUCTOR)
    accepted = world.submit(learner, WRONG_PROGRAM)
    world.login("instructor")
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    top, below = world.task_version.criteria
    assert run.skipped_criteria == (below.id,), "前提: 下位が打ち切られている"
    return accepted.submission.id, run, top, below


def _saved(world: World, run):
    with world.database.unit_of_work() as uow:
        reviews = uow.reviews.reviews_for_run(run.id)
    assert len(reviews) == 1
    return reviews[0]


def _post(world: World, submission_id, form: dict[str, str]):
    return world.client.post(
        f"/review/{submission_id}/finalize",
        data={**form, "comment": JUSTIFICATION},
        follow_redirects=False,
    )


@needs_c_compiler
def test_a_kept_gate_finalizes_without_a_level_below(world: World) -> None:
    submission_id, run, top, below = _gated_submission(world)
    machine_top = {s.criterion_id: s.level for s in run.criterion_scores}[top.id]

    response = _post(world, submission_id, {f"level_{top.code}": str(machine_top)})

    assert response.status_code == 303, response.text
    review = _saved(world, run)
    assert review.gated_criteria == (below.id,)
    assert review.adjusted_levels == {}
    assert final_score(run, world.task_version, review).evaluation == 0.0


@needs_c_compiler
def test_a_level_sent_for_a_gated_criterion_is_not_counted(world: World) -> None:
    """**#675 の事例。** 上位が 0% のまま下位に満点を送っても、点は上がらない。"""
    submission_id, run, top, below = _gated_submission(world)
    machine_top = {s.criterion_id: s.level for s in run.criterion_scores}[top.id]
    best = max(level.level for level in below.levels)

    response = _post(
        world,
        submission_id,
        {f"level_{top.code}": str(machine_top), f"level_{below.code}": str(best)},
    )

    assert response.status_code == 303, response.text
    review = _saved(world, run)
    assert below.id not in review.adjusted_levels
    assert review.gated_criteria == (below.id,)
    assert final_score(run, world.task_version, review).evaluation == 0.0


@needs_c_compiler
def test_lifting_the_top_criterion_requires_a_level_below(world: World) -> None:
    submission_id, _run, top, below = _gated_submission(world)
    best_top = max(level.level for level in top.levels)

    response = _post(world, submission_id, {f"level_{top.code}": str(best_top)})

    assert response.status_code == 400
    assert below.code in response.text


@needs_c_compiler
def test_lifting_the_top_criterion_counts_the_level_below(world: World) -> None:
    submission_id, run, top, below = _gated_submission(world)
    best_top = max(level.level for level in top.levels)
    best_below = max(level.level for level in below.levels)

    response = _post(
        world,
        submission_id,
        {f"level_{top.code}": str(best_top), f"level_{below.code}": str(best_below)},
    )

    assert response.status_code == 303, response.text
    review = _saved(world, run)
    assert review.gated_criteria == ()
    assert final_score(run, world.task_version, review).evaluation == 1.0


@needs_c_compiler
def test_the_page_does_not_require_a_level_for_a_gated_criterion(world: World) -> None:
    submission_id, _run, _top, below = _gated_submission(world)

    page = world.client.get(f"/review/{submission_id}/reveal").text

    assert "上位の観点が 0% のため、この観点は評価しません" in page
    radios = [line for line in page.splitlines() if f'name="level_{below.code}"' in line]
    assert radios, "打ち切った観点の入力欄は（隠して）残す ── 上位を引き上げたら使う"
    assert all("required" not in line for line in radios)
