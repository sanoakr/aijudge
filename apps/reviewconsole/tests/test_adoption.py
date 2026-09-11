"""採用提出の決め方（`aijudge_reviewconsole.submissions.adopted_ids`）。

**学習者に見えている採用と、教員が見る採用は同じでなければならない。**
規則は `aijudge_studentweb.progress` にもあり、そちらが正である ── 成績に
採用される提出が画面によって違えば、教員は問い合わせに答えられない。

**決めるのは細い読み出しから**（#253）。一覧は頁ぶんしか持たないので、
そこから決めると「この頁でいちばん高い提出」になる（#255）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core.ids import SubmissionId, TaskId, UserId
from aijudge_reviewconsole.submissions import adopted_ids
from aijudge_submission.protocols import ScoredRow

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
TASK = TaskId("tsk_" + "2" * 32)
LEARNER = UserId("usr_" + "4" * 32)


def _scored(
    attempt: int,
    ratio: float | None,
    *,
    learner: UserId = LEARNER,
    at: datetime | None = None,
) -> ScoredRow:
    return ScoredRow(
        submission_id=SubmissionId(f"sub_{attempt:032d}"),
        learner_id=learner,
        task_id=TASK,
        final_ratio=ratio,
        is_trial=False,
        submitted_at=at or NOW + timedelta(minutes=attempt),
        attempt=attempt,
        graded=ratio is not None,
    )


def _only(attempt: int) -> set[SubmissionId]:
    return {SubmissionId(f"sub_{attempt:032d}")}


def test_a_tie_is_won_by_the_later_submission() -> None:
    """同点なら後に出した提出（`aijudge_studentweb.progress` と同じ規則）。"""
    assert adopted_ids((_scored(1, 1.0), _scored(2, 1.0), _scored(3, 1.0))) == _only(3)


def test_the_answer_does_not_depend_on_the_order_it_is_given() -> None:
    """**並びを変えても同じ提出が採用になる。**

    以前は「先に見つけた方を後で上書きする」書き方で、入力が古い順で
    あることに暗黙に頼っていた。#233 で一覧を新しい順に変えたとき、同点の
    採用が学習者と逆になった ── 3 回とも満点なら、学習者には 3 回目が、
    教員には 1 回目が採用として見えていた（#256）。
    """
    rows = (_scored(1, 1.0), _scored(2, 1.0), _scored(3, 1.0))
    assert adopted_ids(rows) == adopted_ids(tuple(reversed(rows))) == _only(3)


def test_the_highest_score_wins_regardless_of_when_it_was_made() -> None:
    """点が先。時刻は同点のときだけ効く ── 最後の提出が最高点とは限らない。"""
    assert adopted_ids((_scored(1, 1.0), _scored(2, 0.5), _scored(3, 0.9))) == _only(1)


def test_an_unscored_submission_is_never_adopted() -> None:
    """点の無い提出（採点中・保留）は候補にしない ── 0 点の採用に見える。"""
    assert adopted_ids((_scored(1, 0.4), _scored(2, None))) == _only(1)


def test_the_same_instant_is_broken_by_the_attempt_number() -> None:
    """時刻だけでは決まらない ── 同じ時刻に入った提出が偶然で決まる。"""
    rows = (_scored(1, 1.0, at=NOW), _scored(2, 1.0, at=NOW))
    assert adopted_ids(rows) == _only(2)


def test_each_learner_gets_their_own_adopted_submission() -> None:
    """採用は学習者・課題ごと。1 コースに 1 件ではない。"""
    other = UserId("usr_" + "5" * 32)
    rows = (_scored(1, 0.9), _scored(2, 0.4, learner=other))
    assert len(adopted_ids(rows)) == 2
