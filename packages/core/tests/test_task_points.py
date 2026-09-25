"""配点（`TaskVersion.max_score`）の数え方（2026-09-25 決定）。

固定したいのは 3 つ。

版に付く      配点を書いた版の提出は、その版の配点で数える。教員が配点を
              下げても、前の版で取った点は下がらない。
既出の揃え    配点の機能より前の版（書いていない版）は、最初に配点を書いた
              版の値で数える。学生に見せていたのは割合だけだった。
書き忘れ      配点を書いた後に書かずに直した版は、直前に書いた値を引き継ぐ
              （既定の 100 に戻らない）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    Provenance,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    effective_max_score,
    max_scores_by_version,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId

TASK = TaskId("tsk_" + "1" * 32)
OTHER = TaskId("tsk_" + "2" * 32)


def _version(number: int, max_score: float = 100.0, *, declared: bool, task=TASK) -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId(f"tsv_{str(task)[-4:]}{number:028d}"),
        task_id=task,
        version=number,
        subject_profile="cs_lang_c_intro",
        statement="## 問題 ##",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "3" * 32),
                code="correctness",
                title="正しさ",
                description="テストで判定する",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="動かない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="動く", score_ratio=1.0),
                ),
            ),
        ),
        max_score=max_score,
        points_declared=declared,
        provenance=Provenance(authored_by=UserId("usr_" + "4" * 32)),
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
    )


def test_a_declared_version_keeps_its_own_points() -> None:
    v2 = _version(2, 50, declared=True)
    v3 = _version(3, 40, declared=True)
    assert effective_max_score(v2, [v2, v3]) == 50
    assert effective_max_score(v3, [v2, v3]) == 40


def test_versions_before_points_take_the_first_declared_value() -> None:
    legacy = _version(1, declared=False)
    first = _version(2, 20, declared=True)
    later = _version(3, 30, declared=True)
    assert effective_max_score(legacy, [legacy, first, later]) == 20


def test_an_undeclared_revision_inherits_the_points_before_it() -> None:
    first = _version(1, 20, declared=True)
    changed = _version(2, 30, declared=True)
    forgot = _version(3, declared=False)
    assert effective_max_score(forgot, [first, changed, forgot]) == 30


def test_without_any_points_the_default_stays() -> None:
    legacy = _version(1, declared=False)
    assert effective_max_score(legacy, [legacy]) == 100


def test_the_table_covers_every_version_of_every_task() -> None:
    legacy = _version(1, declared=False)
    first = _version(2, 20, declared=True)
    other = _version(1, 10, declared=True, task=OTHER)
    table = max_scores_by_version([legacy, first, other])
    assert table == {legacy.id: 20, first.id: 20, other.id: 10}
