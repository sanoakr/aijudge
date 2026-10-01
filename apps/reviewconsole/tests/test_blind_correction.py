"""blind 採点の訂正（ADR 0031）。

人の採点には押し間違いがあり、AI の判定と食い違って初めて気づくことが多い。
固定したいのは 5 つ。

追記する          元の blind 採点は残り、訂正が別の記録として足される
画面と測定が従う  確定の画面・一致の判定・観測は訂正後の段階を使う
偏りを隠さない    観測に「訂正した」が残り、測定の報告に件数が出る
直せる人を絞る    採点した本人と担当教員だけ。理由は必須
一致したら確認    訂正して AI と一致すれば、blind の保存と同じく確認まで済む
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_console import (
    PROFILE,
    World,
    _agree_form,
    _instructor_and_submission,
    needs_c_compiler,
)
from test_console import blind_world as blind_world  # フィクスチャを借りる

from aijudge_core import Role

REASON = "一部（50%）を選ぶつもりが、達成を押していました。"


def _machine(world: World, submission_id) -> dict:
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
    return {score.criterion_id: score.level for score in run.criterion_scores}


def _levels_form(world: World, levels: dict) -> dict[str, str]:
    form = _agree_form(world, levels)
    form.pop("comment")
    return form


def _mis_marked(world: World):
    """AI と 1 観点だけ食い違う blind 採点を付けた提出（押し間違いの想定）。"""
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    machine = _machine(world, accepted.submission.id)
    criterion = next(iter(machine))
    wrong = machine | {criterion: 0 if machine[criterion] else 1}
    world.client.post(f"/review/{accepted.submission.id}/blind", data=_levels_form(world, wrong))
    return accepted.submission.id, machine, wrong


@needs_c_compiler
def test_a_correction_is_appended_and_the_original_mark_stays(blind_world: World) -> None:
    submission_id, machine, wrong = _mis_marked(blind_world)

    response = blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, machine) | {"reason": REASON},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    with blind_world.database.unit_of_work() as uow:
        original = uow.reviews.find_blind_mark(submission_id)
        corrections = uow.reviews.blind_corrections(submission_id)
        run = uow.runs.latest_for(submission_id)
        review = uow.reviews.find_review_for_run(run.id)
    assert original.levels == wrong, "元の blind 採点は書き換えない"
    assert len(corrections) == 1
    assert corrections[0].levels == machine and corrections[0].previous_levels == wrong
    # 訂正して一致したので、確認まで済む（ADR 0030 と同じ扱い）。
    assert review is not None and review.agreed


@needs_c_compiler
def test_the_observation_uses_the_corrected_level_and_says_so(
    blind_world: World, monkeypatch
) -> None:
    from aijudge_reviewconsole import app as console_app

    saved: list = []
    real = console_app.Console.refresh_observations

    def spy(self, *args, **kwargs):
        saved.append(kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(console_app.Console, "refresh_observations", spy)
    submission_id, machine, _wrong = _mis_marked(blind_world)
    blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, machine) | {"reason": REASON},
    )

    last = saved[-1]
    assert last["blind_corrected"] is True
    assert last["mark"].levels == machine


@needs_c_compiler
def test_the_reveal_page_offers_the_correction_and_shows_it_happened(blind_world: World) -> None:
    submission_id, machine, _wrong = _mis_marked(blind_world)
    body = blind_world.client.get(f"/review/{submission_id}/reveal").text
    assert 'id="blind-correct"' in body

    blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, machine) | {"reason": REASON},
    )
    body = blind_world.client.get(f"/review/{submission_id}/reveal").text
    assert "1 回訂正" in body and REASON in body


@needs_c_compiler
def test_only_the_marker_or_an_instructor_may_correct(blind_world: World) -> None:
    submission_id, machine, _wrong = _mis_marked(blind_world)
    blind_world.register("ta", role=Role.ASSISTANT)
    blind_world.login("ta")

    response = blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, machine) | {"reason": REASON},
        follow_redirects=False,
    )

    assert response.status_code == 403
    with blind_world.database.unit_of_work() as uow:
        assert uow.reviews.blind_corrections(submission_id) == ()


@needs_c_compiler
def test_a_correction_needs_a_reason_and_a_change(blind_world: World) -> None:
    submission_id, machine, wrong = _mis_marked(blind_world)

    short = blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, machine) | {"reason": "間違い"},
        follow_redirects=False,
    )
    same = blind_world.client.post(
        f"/review/{submission_id}/blind/correct",
        data=_levels_form(blind_world, wrong) | {"reason": REASON},
        follow_redirects=False,
    )

    assert (short.status_code, same.status_code) == (400, 400)


def test_the_measurement_counts_the_corrected_blind_marks() -> None:
    from aijudge_analytics import summarize
    from aijudge_observation import Observation

    def observation(submission: str, *, corrected: bool) -> Observation:
        return Observation(
            subject_profile=PROFILE,
            task_name="t",
            submission=submission,
            criterion_code="readability",
            levels=(0, 1, 2, 3),
            machine_level=3,
            human_level=3,
            blind=True,
            blind_corrected=corrected,
            observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        )

    summary = summarize([observation("a", corrected=True), observation("b", corrected=False)])

    assert summary.blind_submission_count == 2
    assert summary.corrected_blind_submission_count == 1
