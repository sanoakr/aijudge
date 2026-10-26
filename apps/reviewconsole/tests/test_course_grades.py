"""コースの成績の一覧と CSV（`aijudge_reviewconsole.grades`・2026-10-26）。

固定したいのは 5 つ。

採用          欄の点は採用提出（`adopted_ids`）── 学習者の画面と同じ規則
配点の無い課題   割合（0.0〜1.0）を 1 点満点として合計に足す
分母          対象外（出題先のグループの外）と公開前は数えない。未提出は 0 点で数える
絞り込み       部分一致の検索・問題セット・グループ・残っているもの・並び
CSV          教員だけ、BOM 付き、式として読まれる値を無害化、書き出しを監査に残す
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_console import COURSE, TENANT, World, _instructor_and_submission, needs_c_compiler
from test_console import world as world  # フィクスチャを借りる

from aijudge_core import Role, Task
from aijudge_core.ids import CourseGroupId, CourseId, SubmissionId, TaskId, TaskVersionId, UserId
from aijudge_reviewconsole.grades import (
    Column,
    GradeFilters,
    Learner,
    build_gradebook,
    long_csv,
    wide_csv,
)
from aijudge_submission.protocols import ScoredRow

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
CRS = CourseId("crs_" + "9" * 32)
POINTED = Task(id=TaskId("tsk_" + "1" * 32), course_id=CRS, title="p1", session=1)
RATIO = Task(id=TaskId("tsk_" + "2" * 32), course_id=CRS, title="p2", session=1)
RETEST = Task(
    id=TaskId("tsk_" + "3" * 32),
    course_id=CRS,
    title="追試",
    unit="exam",
    audience_group_ids=(CourseGroupId("grp_" + "1" * 32),),
)
LATER = Task(
    id=TaskId("tsk_" + "4" * 32),
    course_id=CRS,
    title="次回",
    session=2,
    opens_at=NOW + timedelta(days=7),
)
V_POINTED = TaskVersionId("tv_" + "1" * 32)
V_RATIO = TaskVersionId("tv_" + "2" * 32)
COLUMNS = (
    Column(task=POINTED, unit_key="1", unit_label="第 1 回", max_points=20.0, pointed=True),
    Column(task=RATIO, unit_key="1", unit_label="第 1 回", max_points=1.0, pointed=False),
    Column(task=RETEST, unit_key="exam", unit_label="exam", max_points=1.0, pointed=False),
    Column(task=LATER, unit_key="2", unit_label="第 2 回", max_points=1.0, pointed=False),
)
SCALE = {V_POINTED: 20.0, V_RATIO: 100.0}
ALICE = Learner(UserId("usr_" + "a" * 32), "y260001", "龍谷 花子")
BOB = Learner(UserId("usr_" + "b" * 32), "y260002", "=HYPERLINK(1)")


def _row(
    index: int,
    learner: Learner,
    task: Task,
    ratio: float | None,
    *,
    version: TaskVersionId = V_POINTED,
    finalized: bool = False,
    trial: bool = False,
    graded: bool = True,
    minutes: int = 0,
) -> ScoredRow:
    return ScoredRow(
        submission_id=SubmissionId(f"sub_{index:032d}"),
        learner_id=learner.user_id,
        task_id=task.id,
        final_ratio=ratio,
        is_trial=trial,
        submitted_at=NOW - timedelta(hours=10) + timedelta(minutes=minutes),
        attempt=index,
        graded=graded,
        finalized=finalized,
        task_version_id=version,
    )


def _book(rows, filters: GradeFilters | None = None, learners=(ALICE, BOB)):
    return build_gradebook(COLUMNS, learners, rows, SCALE, now=NOW, filters=filters)


def test_the_adopted_score_and_unpointed_tasks_add_up() -> None:
    rows = (
        _row(1, ALICE, POINTED, 0.5, minutes=1),
        _row(2, ALICE, POINTED, 0.9, finalized=True, minutes=2),
        _row(3, ALICE, POINTED, 0.7, minutes=3),
        _row(4, ALICE, RATIO, 0.85, version=V_RATIO),
        # 教員の試しの提出は数えない（#108）。
        _row(5, BOB, POINTED, 1.0, trial=True),
    )
    alice, bob = _book(rows).rows

    first, second, retest, later = alice.cells
    assert first.state == "finalized" and first.points == 18.0, "最高点の提出・配点 20"
    assert second.points == 0.85 and second.max_points == 1.0, "割合を 1 点満点で"
    assert retest.state == "excluded", "追試の名簿に入っていない"
    assert later.state == "upcoming"
    assert alice.total.earned == 18.85
    assert alice.total.possible == 21.0, "対象外と公開前は分母に入れない"

    assert [c.state for c in bob.cells[:2]] == ["missing", "missing"]
    assert bob.total.earned == 0.0 and bob.total.possible == 21.0, "未提出は 0 点で数える"


def test_a_submission_without_a_score_is_withheld_or_grading() -> None:
    rows = (
        _row(1, ALICE, POINTED, None),
        _row(2, BOB, POINTED, None, graded=False),
    )
    alice, bob = _book(rows).rows
    assert alice.cells[0].state == "withheld"
    assert bob.cells[0].state == "grading"
    assert alice.total.possible == 21.0, "点が無くても出題はされている"


def test_the_group_decides_who_the_retest_counts_for() -> None:
    member = Learner(
        UserId("usr_" + "c" * 32),
        "y260003",
        "追試 太郎",
        groups=frozenset(RETEST.audience_group_ids),
    )
    book = _book((), learners=(ALICE, member))
    assert book.rows[1].cells[2].state == "missing"
    only = _book((), GradeFilters(group=str(RETEST.audience_group_ids[0])), (ALICE, member))
    assert [r.login for r in only.rows] == ["y260003"]


def test_search_unit_flag_and_sort() -> None:
    rows = (_row(1, ALICE, POINTED, 1.0), _row(2, BOB, POINTED, 0.5))
    assert [r.login for r in _book(rows, GradeFilters(q="花子")).rows] == ["y260001"]
    assert [r.login for r in _book(rows, GradeFilters(q="0002")).rows] == ["y260002"]

    unit = _book(rows, GradeFilters(unit="1"))
    assert [c.task.title for c in unit.columns] == ["p1", "p2"]
    assert unit.rows[0].total.possible == 21.0

    unfinalized = _book(
        (_row(1, ALICE, POINTED, 1.0, finalized=True), _row(2, BOB, POINTED, 0.5)),
        GradeFilters(flag="unfinalized"),
    )
    assert [r.login for r in unfinalized.rows] == ["y260002"]

    ranked = _book(rows, GradeFilters(sort="total_asc"))
    assert [r.login for r in ranked.rows] == ["y260002", "y260001"]


def test_the_csv_opens_in_excel_and_cannot_run_a_formula() -> None:
    rows = (_row(1, ALICE, POINTED, 0.9), _row(2, BOB, RATIO, 0.5, version=V_RATIO))
    wide = wide_csv(_book(rows))
    assert wide.startswith("﻿"), "BOM が無いと日本語の Excel で化ける"
    lines = wide.lstrip("﻿").splitlines()
    assert "第 1 回 p1（20 点）" in lines[0] and "第 1 回 p2（割合）" in lines[0]
    assert lines[1].startswith("y260001,龍谷 花子,18,,")
    assert "'=HYPERLINK(1)" in lines[2]

    long = long_csv(_book(rows)).lstrip("﻿").splitlines()
    assert long[0].startswith("ログイン名,氏名,問題セット,課題,点,満点,割合,状態")
    assert "y260002,'=HYPERLINK(1),第 1 回,p2,0.5,1,0.5,未確定" in long[2 + 4]


# -- 画面と CSV の経路 ----------------------------------------------------------


@needs_c_compiler
def test_the_page_lists_the_learner_and_the_csv_is_audited(world: World) -> None:
    _instructor_and_submission(world)
    world.worker.run_until_empty()

    page = world.client.get(f"/courses/{COURSE}/grades")
    assert page.status_code == 200
    assert "s2400001" in page.text and 'class="gradebook"' in page.text
    assert "grades.csv" in page.text

    exported = world.client.get(f"/courses/{COURSE}/grades.csv?shape=long")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    assert "s2400001" in exported.content.decode("utf-8-sig")
    with world.database.unit_of_work() as uow:
        actions = [e.action.value for e in uow.audit.list_recent(TENANT, limit=50)]
    assert "grades.exported" in actions


def test_an_assistant_sees_the_page_but_cannot_export(world: World) -> None:
    world.register("s2400001", role=Role.LEARNER)
    world.register("ta", role=Role.ASSISTANT)
    world.login("ta")
    page = world.client.get(f"/courses/{COURSE}/grades")
    assert page.status_code == 200
    assert "grades.csv" not in page.text
    assert world.client.get(f"/courses/{COURSE}/grades.csv").status_code in (403, 404)


def test_a_learner_cannot_open_the_grades(world: World) -> None:
    world.register("s2400001", role=Role.LEARNER)
    world.login("s2400001")
    assert world.client.get(f"/courses/{COURSE}/grades").status_code in (403, 404)
    assert world.client.get(f"/courses/{COURSE}/grades.csv").status_code in (403, 404)
