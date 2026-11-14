"""AI が何も判定しない課題の blind 採点は、一致度の標本に入らない（2026-11-15）。

blind の抽出は AI が判定する課題だけに絞った（`judged_by_ai`）が、それ以前に付いた
採点は消さない（P8）。測定の側でも、決定的評価器だけの課題に残った採点が標本に
数えられないことを固定する。
"""

from __future__ import annotations

from test_human_scored_criteria import CORRECTNESS, DIAGRAM
from test_observation_projection import _deterministic, _project, _run, _task


def test_blind_marks_on_a_task_no_ai_judged_never_enter_the_agreement_sample() -> None:
    task = _task()
    run = _run(
        task,
        (_deterministic(CORRECTNESS, conclusive=True), _deterministic(DIAGRAM, conclusive=False)),
    )
    rows = _project(task, run, human_levels={"correctness": 1, "diagram": 0}, blind=True)
    assert all(row.blind and row.human_level is not None for row in rows.values())
    assert not any(row.usable_for_agreement for row in rows.values())
