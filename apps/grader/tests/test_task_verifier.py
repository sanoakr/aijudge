"""門を実際に走らせて確かめる（S2、ADR 0008 / 設計方針 §5）。

**門 2 が本質である。** 門 1 だけなら、参照解答が通ることしか言えない ──
何を渡しても通るテストケースでも満点になる。ここで固定したいのは
「テストケースが弱い課題を、門が実際に落とすこと」である。

変異の規則そのものは `packages/authoring/tests/test_verification.py` が
実行環境なしで確かめる。こちらは**サンドボックスで本当に走らせる**側。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring import GateOutcome
from aijudge_core import (
    Provenance,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_grader import TaskVerifier
from aijudge_grading import EvaluatorRegistry, load_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = load_profile(REPO_ROOT / "subjects" / "cs_intro_c.yaml")
AUTHOR = UserId("usr_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

# 出力が入力に依存する解答。テストケースが 2 件あれば変異はよく落ちる。
STRONG = """#include <stdio.h>
int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a + b);
    return 0;
}
"""

# **出力が入力に依存しない解答。** 使われない値をいくら壊しても出力は変わらず、
# 変異は生き残る ── テストケースが何も見ていないことの現れである。
WEAK = """#include <stdio.h>
int main(void) {
    int unused = 42;
    int spare = 7;
    int extra = 13;
    int more = 99;
    printf("fixed\\n");
    return 0;
}
"""


def _task(reference: str, cases: tuple[TestCase, ...]) -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="## 課題 ##\n\n書きなさい。",
        reference_solution=reference,
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "4" * 32),
                code="correctness",
                title="出力の正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
                evaluator_id="code_test_runner",
            ),
        ),
        test_cases=cases,
        max_score=100.0,
        provenance=Provenance(authored_by=AUTHOR),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _case(name: str, stdin: str, stdout: str) -> TestCase:
    return TestCase(
        name=name,
        evaluator_id="code_test_runner",
        payload={"input": stdin, "expected": stdout},
    )


def _verifier(**kwargs) -> TaskVerifier:
    return TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE, **kwargs)


@needs_c_compiler
def test_a_task_whose_tests_see_something_passes_both_gates() -> None:
    report = _verifier(mutation_limit=8).verify(
        _task(STRONG, (_case("t1", "2 3\n", "5\n"), _case("t2", "10 1\n", "11\n")))
    )
    assert report.reference_passes is GateOutcome.PASSED
    assert report.gate_two is GateOutcome.PASSED, report.summary()
    assert report.usable


@needs_c_compiler
def test_a_task_whose_tests_see_nothing_is_refused_by_gate_two() -> None:
    """**これが門 2 を置いた理由である。**

    参照解答は通る（門 1 は合格）。だがテストケースは固定の出力しか見て
    いないので、解答の中身を壊しても落ちない。門 1 だけならこの課題は
    採点に使えてしまう。
    """
    report = _verifier(mutation_limit=10).verify(_task(WEAK, (_case("t1", "", "fixed\n"),)))
    assert report.reference_passes is GateOutcome.PASSED
    assert report.gate_two is GateOutcome.FAILED, report.summary()
    assert not report.usable
    # 教員に何を足せばよいか示す（設計原則 P4 を作問にも適用する）。
    assert report.survivors, "生き残った変異を名指しできていない"


@needs_c_compiler
def test_a_reference_solution_that_fails_stops_at_gate_one() -> None:
    """門 1 で落ちたら門 2 は走らせない。

    参照解答が通らない課題で変異を測っても、測っているのは参照解答の誤りで
    ある。しかも変異のぶんだけサンドボックスを無駄に回す。
    """
    report = _verifier().verify(_task(STRONG, (_case("t1", "2 3\n", "999\n"),)))
    assert report.reference_passes is GateOutcome.FAILED
    assert report.mutants_total == 0
    assert not report.usable


def test_a_task_without_a_reference_solution_is_not_verified() -> None:
    """**検査していないことを合格にしない。** 実行環境が要らないので常に走る。"""
    report = _verifier().verify(_task("", (_case("t1", "", ""),)))
    assert report.reference_passes is GateOutcome.NOT_RUN
    assert not report.usable
    assert "検査していません" in report.summary()
