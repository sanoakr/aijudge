"""採用提出の決め方（`aijudge_reviewconsole.submissions._mark_adopted`）。

**学習者に見えている採用と、教員が見る採用は同じでなければならない。**
規則は `aijudge_studentweb.progress` にもあり、そちらが正である ── 成績に
採用される提出が画面によって違えば、問い合わせに答えられない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core import Role, Submission, Task
from aijudge_core.ids import CourseId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_reviewconsole.submissions import Row, _mark_adopted

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
COURSE = CourseId("crs_" + "1" * 32)
TASK = TaskId("tsk_" + "2" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
LEARNER = UserId("usr_" + "4" * 32)


def _row(attempt: int, score: float) -> Row:
    # 提出済みの文書は成果物を要求するので、下書きのまま作る ── ここで見るのは
    # 並べ方だけで、`_mark_adopted` は `submitted_at` が無ければ `created_at` を
    # 見る（実データでも採点前の提出がこの形になる）。
    submission = Submission(
        id=SubmissionId(f"sub_{attempt:032d}"),
        learner_id=LEARNER,
        task_version_id=VERSION,
        attempt=attempt,
        submitted_as=Role.LEARNER,
        created_at=NOW + timedelta(minutes=attempt),
    )
    return Row(
        submission=submission,
        run=None,
        task=Task(id=TASK, course_id=COURSE, title="例題"),
        version=None,  # type: ignore[arg-type]
        learner=None,
        role=Role.LEARNER,
        score=score,
        finalized_by=None,
        finalized_by_login="",
        contested=False,
    )


def _adopted_attempt(rows: list[Row]) -> int:
    marked = _mark_adopted(rows)
    return next(row.submission.attempt for row in marked if row.adopted)


def test_a_tie_is_won_by_the_later_submission() -> None:
    """同点なら後に出した提出（`aijudge_studentweb.progress` と同じ規則）。"""
    assert _adopted_attempt([_row(1, 1.0), _row(2, 1.0), _row(3, 1.0)]) == 3


def test_the_answer_does_not_depend_on_the_order_it_is_given() -> None:
    """**並びを変えても同じ提出が採用になる。**

    以前はここが「先に見つけた方を後で上書きする」書き方で、入力が古い順で
    あることに暗黙に頼っていた。#233 で一覧を新しい順に変えたとき、同点の
    採用が学習者と逆になった ── 3 回とも満点なら、学習者には 3 回目が、
    教員には 1 回目が採用として見えていた。
    """
    oldest_first = [_row(1, 1.0), _row(2, 1.0), _row(3, 1.0)]
    newest_first = list(reversed([_row(1, 1.0), _row(2, 1.0), _row(3, 1.0)]))
    assert _adopted_attempt(oldest_first) == _adopted_attempt(newest_first) == 3


def test_the_highest_score_wins_regardless_of_when_it_was_made() -> None:
    """点が先。時刻は同点のときだけ効く。"""
    assert _adopted_attempt([_row(1, 1.0), _row(2, 0.5), _row(3, 0.9)]) == 1


def test_an_unscored_submission_is_never_adopted() -> None:
    """点の無い提出（採点中・保留）は候補にしない ── 0 点の採用に見える。"""
    rows = _mark_adopted([_row(1, 0.4), _row(2, None)])  # type: ignore[arg-type]
    assert [row.adopted for row in rows] == [True, False]
