"""blind 採点が AI と一致したら、確定の画面を飛ばして確定する（2026-10-01）。

教員は AI を見る前に自分で段階を付けている。全観点で AI と同じなら、確定の画面で
同じ段階を選び直して根拠を打つのは手間でしかない。固定したいのは 5 つ。

一致したら確定する     教員の確認と確定の 2 つが書かれ、根拠はルーブリックの記述
次の 1 件へ進む        順に採点しているなら、確定の画面ではなく次の blind へ
食い違えば画面へ        1 観点でも違えば、今までどおり確定の画面で教員が決める
減点があれば画面へ      免除するかどうかは教員の判断（ADR 0013 §4）
根拠の初期値            確定の画面の根拠の欄には、選ばれている段階の記述が入る
"""

from __future__ import annotations

from test_console import (
    COURSE,
    World,
    _agree_form,
    _instructor_and_submission,
    needs_c_compiler,
)
from test_console import blind_world as blind_world  # フィクスチャを借りる

from aijudge_core import FinalizationSource
from aijudge_course_admin.rubric_text import AGREED_PREFIX, level_lines, rubric_justification


def _machine(world: World, submission_id) -> dict:
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
    return {score.criterion_id: score.level for score in run.criterion_scores}


def _records(world: World, submission_id):
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        return (
            uow.reviews.find_review_for_run(run.id),
            uow.reviews.find_finalization_for_run(run.id),
        )


def _blind_form(world: World, levels: dict) -> dict[str, str]:
    form = _agree_form(world, levels)
    form.pop("comment")
    return form


@needs_c_compiler
def test_an_agreeing_blind_mark_finalizes_without_the_reveal_page(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    machine = _machine(blind_world, accepted.submission.id)

    response = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind?from=blind",
        data=_blind_form(blind_world, machine),
        follow_redirects=False,
    )

    assert response.status_code == 303
    # 待ちはこの 1 件だけなので、一覧に戻る。
    assert response.headers["location"].endswith(f"/courses/{COURSE}/blind")
    review, finalization = _records(blind_world, accepted.submission.id)
    assert review is not None and review.agreed
    assert review.comment.startswith(AGREED_PREFIX)
    assert finalization is not None
    assert finalization.source is FinalizationSource.INSTRUCTOR_REVIEW
    # 確定したことを一度だけ言う。
    page = blind_world.client.get(f"/courses/{COURSE}/blind").text
    assert "一致したので確定しました" in page
    assert "一致したので確定しました" not in blind_world.client.get(f"/courses/{COURSE}/blind").text


@needs_c_compiler
def test_an_agreeing_mark_moves_on_to_the_next_blind_submission(blind_world: World) -> None:
    from aijudge_core import Role

    first_learner = blind_world.register("s2400001", role=Role.LEARNER)
    second_learner = blind_world.register("s2400002", role=Role.LEARNER)
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    first = blind_world.submit(first_learner)
    second = blind_world.submit(second_learner)
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()

    response = blind_world.client.post(
        f"/review/{first.submission.id}/blind?from=blind",
        data=_blind_form(blind_world, _machine(blind_world, first.submission.id)),
        follow_redirects=False,
    )

    assert response.headers["location"].endswith(f"/review/{second.submission.id}/blind?from=blind")
    page = blind_world.client.get(response.headers["location"]).text
    assert "一致したので確定しました" in page


@needs_c_compiler
def test_a_differing_blind_mark_still_goes_to_the_reveal_page(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    machine = _machine(blind_world, accepted.submission.id)
    criterion = next(iter(machine))
    differing = machine | {criterion: 0 if machine[criterion] else 1}

    response = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind?from=blind",
        data=_blind_form(blind_world, differing),
        follow_redirects=False,
    )

    assert "/reveal" in response.headers["location"]
    assert _records(blind_world, accepted.submission.id) == (None, None)


@needs_c_compiler
def test_a_late_submission_goes_to_the_reveal_page_even_when_agreeing(
    blind_world: World, monkeypatch
) -> None:
    """**減点があれば一致しても画面へ。** 免除するかどうかは教員が決める。"""
    from datetime import UTC, datetime

    from aijudge_core import LatePenalty
    from aijudge_reviewconsole import app as console_app

    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    machine = _machine(blind_world, accepted.submission.id)
    real = console_app._load
    penalty = LatePenalty(
        ratio=0.5,
        hours_late=1.0,
        due_at=datetime(2026, 10, 1, tzinfo=UTC),
        submitted_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
        reason="締切を 1.0 時間超えています",
    )

    def late(*args, **kwargs):
        context = real(*args, **kwargs)
        context.run = context.run.model_copy(update={"penalty": penalty})
        return context

    monkeypatch.setattr(console_app, "_load", late)
    response = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind?from=blind",
        data=_blind_form(blind_world, machine),
        follow_redirects=False,
    )

    assert "/reveal" in response.headers["location"]
    assert _records(blind_world, accepted.submission.id) == (None, None)


@needs_c_compiler
def test_the_reveal_page_starts_with_the_rubric_text_of_the_chosen_levels(
    blind_world: World,
) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    machine = _machine(blind_world, accepted.submission.id)
    criterion = next(iter(machine))
    differing = machine | {criterion: 0 if machine[criterion] else 1}
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data=_blind_form(blind_world, differing),
    )

    body = blind_world.client.get(f"/review/{accepted.submission.id}/reveal").text

    # **blind の段階**（最初に選ばれている方）の記述が入る。
    expected = level_lines(blind_world.task_version.criteria, differing)
    assert expected and all(line in body for line in expected)
    assert "data-rubric-line=" in body


def test_the_text_lists_each_chosen_level_and_marks_an_agreement() -> None:
    from aijudge_core import RubricCriterion, RubricLevel
    from aijudge_core.ids import CriterionId

    criterion = RubricCriterion(
        id=CriterionId("crt_" + "6" * 32),
        code="certificate",
        title="認定証が確認できる",
        description="学籍番号入りの認定証",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="提出が無い", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="学籍番号が読めない", score_ratio=0.5),
            RubricLevel(level=2, label="達成", descriptor="学籍番号まで読める", score_ratio=1.0),
        ),
    )

    assert rubric_justification([criterion], {criterion.id: 1}) == (
        "認定証が確認できる: 一部（50%） ── 学籍番号が読めない"
    )
    assert rubric_justification([criterion], {criterion.id: 0}, agreed=True) == (
        f"{AGREED_PREFIX}\n認定証が確認できる: 未達（0%） ── 提出が無い"
    )
