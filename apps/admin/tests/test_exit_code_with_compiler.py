"""終了コードの期待を、実際にコンパイルして走らせて確かめる（2026-10-18）。

判定の規則は `evaluators/code_test_runner/tests/test_exit_expectation.py` が
実行なしで固定する。こちらは**サンドボックスで本当に走らせる**側で、門 1
（参照解答が全ケースを通る）が採点と同じ判定をすることも同時に見る ──
`return 1;` で終える参照解答は、`nonzero` のケースでなら門を通る。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core import Provenance, RubricCriterion, RubricLevel, TaskVersion, TestCase
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_course_admin.task_verifier import TaskVerifier
from aijudge_grading import EvaluatorRegistry, load_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = load_profile(REPO_ROOT / "subjects" / "cs_lang_c_intro.yaml")
AUTHOR = UserId("usr_" + "1" * 32)

pytestmark = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

# 正の整数なら 2 倍を出す。**それ以外は何も出力せず 1 で終える**（C の慣習どおり）。
REFERENCE = """#include <stdio.h>
#include <stdlib.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1 || n <= 0) {
        return EXIT_FAILURE;
    }
    printf("%d\\n", n * 2);
    return 0;
}
"""

# 不正な入力でも 0 で終える（エラーを成功として返す）。
ALWAYS_ZERO = REFERENCE.replace("return EXIT_FAILURE;", "return 0;")

# 出力したあと segfault する。
CRASHES = REFERENCE.replace("return EXIT_FAILURE;", "{ int *p = 0; *p = 1; }")


def _task(cases: tuple[TestCase, ...]) -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "2" * 32),
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="## 課題 ##\n\n書きなさい。",
        reference_solution=REFERENCE,
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
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


def _cases(bad_input_exit: str | None) -> tuple[TestCase, ...]:
    bad: dict[str, object] = {"input": "-3\n", "expected": ""}
    if bad_input_exit is not None:
        bad["exit_code"] = bad_input_exit
    return (
        TestCase(
            name="ok",
            evaluator_id="code_test_runner",
            payload={"input": "4\n", "expected": "8\n"},
        ),
        TestCase(name="bad_input", evaluator_id="code_test_runner", payload=bad),
    )


def _passes(source: str, bad_input_exit: str | None) -> tuple[bool, str]:
    verifier = TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE)
    return verifier.passes(_task(_cases(bad_input_exit)), source)


def test_an_error_exit_fails_where_the_case_expects_zero() -> None:
    """書かなければ従来どおり。慣習どおりの解答が落ちる（これを直したかった）。"""
    passed, _ = _passes(REFERENCE, None)
    assert not passed


def test_an_error_exit_passes_where_the_case_expects_nonzero() -> None:
    passed, detail = _passes(REFERENCE, "nonzero")
    assert passed, detail


def test_a_zero_exit_fails_where_the_case_expects_nonzero() -> None:
    passed, detail = _passes(ALWAYS_ZERO, "nonzero")
    assert not passed
    assert "0 以外の終了コード" in detail


@pytest.mark.parametrize("expectation", ["nonzero", "any"])
def test_a_crash_never_passes(expectation: str) -> None:
    passed, _ = _passes(CRASHES, expectation)
    assert not passed
