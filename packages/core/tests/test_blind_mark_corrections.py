"""`corrected_mark` の「最新の訂正が効く」を固定する（#684・ADR 0031）。

blind 採点を 2 回訂正した提出で、最初の訂正の段階が使われると、一致度の測定と
確定画面の初期値が古い値になる。`max` を `min` に変えてもスイートは通っていた
（`corrected_mark` を直接呼ぶテストが無かった）。

採点者と時刻は元の blind 採点のまま残す。blind 採点をしたのはその人・その時刻で、
訂正は別の事実として `BlindMarkCorrection` が持つ。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import BlindMark, BlindMarkCorrection, corrected_mark
from aijudge_core.ids import BlindMarkCorrectionId, CriterionId, SubmissionId, UserId

MARKED_AT = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SUBMISSION = SubmissionId("sub_" + "b" * 32)
GRADER = UserId("usr_" + "1" * 32)
CORRECTOR = UserId("usr_" + "2" * 32)
C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)

MARK = BlindMark(
    submission_id=SUBMISSION,
    grader_id=GRADER,
    levels={C1: 0, C2: 0},
    marked_at=MARKED_AT,
)


def _correction(
    suffix: str, levels: dict[CriterionId, int], *, after: timedelta
) -> BlindMarkCorrection:
    return BlindMarkCorrection(
        id=BlindMarkCorrectionId("bmc_" + suffix * 32),
        submission_id=SUBMISSION,
        corrected_by=CORRECTOR,
        levels=levels,
        previous_levels={C1: 0, C2: 0},
        reason="押し間違いに気づいたので直します。",
        corrected_at=MARKED_AT + after,
    )


FIRST = _correction("1", {C1: 1, C2: 0}, after=timedelta(hours=1))
SECOND = _correction("2", {C1: 2, C2: 1}, after=timedelta(hours=2))


def test_without_corrections_the_mark_is_returned_as_is() -> None:
    mark, corrected = corrected_mark(MARK, ())
    assert mark == MARK
    assert not corrected


@pytest.mark.parametrize(
    "corrections", [(FIRST, SECOND), (SECOND, FIRST)], ids=["in-order", "reversed"]
)
def test_the_latest_correction_wins_whatever_order_it_arrives_in(
    corrections: tuple[BlindMarkCorrection, ...],
) -> None:
    """渡す順に依らず、`corrected_at` の新しい訂正の段階が効く。"""
    mark, corrected = corrected_mark(MARK, corrections)
    assert corrected
    assert mark.levels == SECOND.levels


def test_the_grader_and_the_time_stay_those_of_the_blind_mark() -> None:
    """訂正した人・時刻は元の採点に移さない（ADR 0031）。"""
    mark, _ = corrected_mark(MARK, (FIRST, SECOND))
    assert mark.grader_id == GRADER
    assert mark.marked_at == MARKED_AT
    assert mark.submission_id == SUBMISSION


def test_corrections_at_the_same_instant_are_broken_by_id() -> None:
    """同時刻の訂正は ID の大きい方が効く。決め方が無いと、読むたびに値が変わりうる。"""
    low = _correction("3", {C1: 1, C2: 1}, after=timedelta(hours=3))
    high = _correction("4", {C1: 2, C2: 2}, after=timedelta(hours=3))

    for corrections in ((low, high), (high, low)):
        mark, _ = corrected_mark(MARK, corrections)
        assert mark.levels == high.levels
