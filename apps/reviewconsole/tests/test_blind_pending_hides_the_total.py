"""blind 採点を待つ提出の行に、AI の総合点を出さない（2026-11-14）。

blind 採点は、採点者が AI の判定を見る前に自分で段階を付けるためのもの（ADR 0005）。
blind の画面そのものは判定を出さないが、blind 待ちの提出が並ぶ一覧に総合点が出ていると、
画面に入る前に判定が読める。観点が少ないルーブリックでは総合点から段階がほぼ逆算できる。

固定したいのは 3 つ。

出さない   blind 待ちの行は、再確認の依頼・提出一覧・確定処理のどれでも総合点を出さない
理由を言う  代わりに「blind 採点後に表示」と出す（空欄だと壊れて見える）
抽出外は出す blind の対象でない提出は、これまでどおり総合点を出す
"""

from __future__ import annotations

import re

from test_console import COURSE, World, _agree_form, _request_review, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role

HIDDEN_NOTE = "blind 採点後に表示"


def _scores_in(body: str, submission_id: object) -> list[str]:
    """その提出の行（`<tr>` から次の `</tr>` まで）に出ている「NN%」。"""
    short = str(submission_id)
    rows = re.findall(r"<tr\b.*?</tr>", body, re.S)
    mine = [row for row in rows if short in row]
    assert mine, "その提出の行が一覧に無い"
    return re.findall(r"\d+%", " ".join(mine))


@needs_c_compiler
def test_the_request_queue_hides_the_total_of_a_blind_pending_submission(
    blind_world: World,
) -> None:
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    accepted = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()
    _request_review(blind_world, accepted.submission.id)

    body = blind_world.client.get(f"/courses/{COURSE}/queue").text

    assert HIDDEN_NOTE in body
    assert _scores_in(body, accepted.submission.id) == []


@needs_c_compiler
def test_the_submission_list_hides_the_total_of_a_blind_pending_submission(
    blind_world: World,
) -> None:
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    accepted = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()

    body = blind_world.client.get(f"/courses/{COURSE}/submissions").text

    assert "blind 採点待ち" in body
    assert HIDDEN_NOTE in body
    assert _scores_in(body, accepted.submission.id) == []


@needs_c_compiler
def test_the_total_comes_back_once_the_blind_mark_is_given(blind_world: World) -> None:
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    accepted = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()

    with blind_world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    form = _agree_form(blind_world, {s.criterion_id: s.level for s in run.criterion_scores})
    form.pop("comment")
    blind_world.client.post(f"/review/{accepted.submission.id}/blind", data=form)

    body = blind_world.client.get(f"/courses/{COURSE}/submissions").text

    assert HIDDEN_NOTE not in body
    assert _scores_in(body, accepted.submission.id) != []


@needs_c_compiler
def test_a_submission_outside_the_blind_sample_keeps_its_total(world: World) -> None:
    """抽出率 0 の世界では、どの提出も blind の対象ではない。点は今までどおり出す。"""
    world.register("instructor", role=Role.INSTRUCTOR)
    accepted = world.submit(world.register("s2400001", role=Role.LEARNER))
    world.login("instructor")
    world.worker.run_until_empty()

    body = world.client.get(f"/courses/{COURSE}/submissions").text

    assert HIDDEN_NOTE not in body
    assert _scores_in(body, accepted.submission.id) != []


@needs_c_compiler
def test_the_finalize_rows_carry_the_blind_pending_mark(blind_world: World) -> None:
    """確定処理の一覧（`finalize.html`）は、この印で総合点を出し分ける。"""
    from aijudge_reviewconsole.app import _open_rows

    blind_world.register("instructor", role=Role.INSTRUCTOR)
    accepted = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.worker.run_until_empty()
    with blind_world.database.unit_of_work() as uow:
        course = uow.identity.get_course(COURSE)

    (row,) = _open_rows(blind_world.console, course)
    assert row["submission"].id == accepted.submission.id
    assert row["needs_blind"] is True
