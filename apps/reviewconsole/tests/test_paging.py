"""提出一覧の頁送り（#255）。

**一覧に上限が要るのは描くからで、図に上限は要らない**（#253）。頁で切れる
ようになったので、一覧は 1 頁ぶんしか描かない ── そのとき壊れやすいのは、
**行をまたいで決まる事実**である（採用提出・総数・分布）。ここではそれが
頁に追従しないことを見る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core.ids import SubmissionId, TaskId, UserId
from aijudge_reviewconsole.submissions import PAGE_SIZE, Page, adopted_ids
from aijudge_submission.protocols import ScoredRow

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
TASK = TaskId("tsk_" + "2" * 32)
LEARNER = UserId("usr_" + "4" * 32)


def _scored(attempt: int, ratio: float | None, *, learner: UserId = LEARNER) -> ScoredRow:
    return ScoredRow(
        submission_id=SubmissionId(f"sub_{attempt:032d}"),
        learner_id=learner,
        task_id=TASK,
        final_ratio=ratio,
        is_trial=False,
        submitted_at=NOW + timedelta(minutes=attempt),
        attempt=attempt,
        graded=ratio is not None,
    )


def test_a_page_reports_where_it_is_without_following_the_page() -> None:
    """**総数は頁に追従しない。** 追従すると、コースの大きさが送るたびに変わる。"""
    page = Page(number=3, total=250)
    assert page.pages == 3
    assert (page.first, page.last) == (201, 250)
    assert page.has_prev and not page.has_next


def test_an_empty_result_still_has_one_page() -> None:
    """0 件でも「0 / 0 頁」とは出さない ── 頁は 1 から数える。"""
    page = Page(number=1, total=0)
    assert page.pages == 1
    assert (page.first, page.last) == (0, 0)
    assert not page.has_prev and not page.has_next


def test_the_page_size_is_a_hundred() -> None:
    """携帯では 1 件 1 枚（約 100px）なので、200 だと 1 頁が 2 万ピクセルになる。"""
    assert PAGE_SIZE == 100


def test_adoption_is_decided_across_the_whole_course_not_one_page() -> None:
    """**採用は頁の中では決まらない**（#255・#256）。

    1 頁ぶんの行から決めると「この頁でいちばん高い提出」になる。採用は
    学習者・課題ごとにコース全体で決まる事実なので、細い読み出し
    （`scored_for_course`・打ち切らない）から決める。
    """
    rows = tuple(_scored(attempt, 0.5) for attempt in range(1, PAGE_SIZE + 51))
    best = _scored(PAGE_SIZE + 60, 1.0)
    assert adopted_ids((*rows, best)) == {best.submission_id}
