"""blind 採点と確定処理の一覧は、課題ごとにまとめて並ぶ（2026-10-01）。

提出の古い順に 1 列で並べると、1 件ずつ進む帯の「次へ」が課題をまたいで
行き来し、採点の基準を問題ごとに持ち替えることになる。帯は一覧と同じ行を
読むので、並びを固定すれば「次へ」の順も固定される。
"""

from __future__ import annotations

from aijudge_core import Task
from aijudge_core.ids import CourseId, TaskId
from aijudge_reviewconsole.app import _in_task_order, _task_groups

COURSE = CourseId("crs_" + "1" * 32)


def _task(key: str, *, unit: str, position: int) -> Task:
    return Task(
        id=TaskId("tsk_" + key * 32),
        course_id=COURSE,
        title=f"{unit} p{position}",
        unit=unit,
        session=int(unit[-1]),
        position=position,
    )


def _row(task: Task | None, name: str) -> dict:
    # 課題は行ごとに引き直される ── 同じ課題でも別の物になる形で組む。
    return {"task": None if task is None else task.model_copy(), "name": name}


def test_rows_follow_the_task_order_and_keep_submission_order_within() -> None:
    p1 = _task("a", unit="ex1", position=1)
    p2 = _task("b", unit="ex1", position=2)
    q1 = _task("c", unit="ex2", position=1)
    # 提出の古い順（`pending_for_course` が返す順）。課題が入り混じっている。
    rows = [
        _row(q1, "q1-first"),
        _row(p2, "p2-first"),
        _row(None, "orphan"),
        _row(p1, "p1-first"),
        _row(p2, "p2-second"),
        _row(p1, "p1-second"),
    ]

    ordered = [row["name"] for row in _in_task_order(rows)]

    assert ordered == [
        "p1-first",
        "p1-second",
        "p2-first",
        "p2-second",
        "q1-first",
        "orphan",
    ], "課題の順に並び、課題の中は提出の古い順、課題の分からない行は最後"


def test_groups_split_on_the_task_id_not_the_object() -> None:
    p1 = _task("a", unit="ex1", position=1)
    p2 = _task("b", unit="ex1", position=2)
    rows = _in_task_order([_row(p1, "1"), _row(p2, "2"), _row(p1, "3")])

    groups = _task_groups(rows)

    assert [(group["task"].id, len(group["rows"])) for group in groups] == [
        (p1.id, 2),
        (p2.id, 1),
    ]
