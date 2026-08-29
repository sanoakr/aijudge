"""解答可能性の検査の規則を固定する（S2、設計方針 §5）。

**教員レビューの前に走り、承認・却下の判断材料になる。** 判定ではない。

固定したいのは 5 つ。

参照解答を渡さない  渡したら何も測っていない。
参照解答と比べない  正しいプログラムは何通りもある。見るのは振る舞い。
落ちても却下しない  曖昧なのか難しいのかを機械は分けられない（P5）。
検査の失敗と区別   モデルが応答しないのは課題の欠陥ではない。
読みを残す         課題文の曖昧さは「読み違え」として現れる。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin import SolvabilityChecker, TaskVerifier, build_packet
from aijudge_authoring.solvability import SolvabilityOutcome
from aijudge_core import Provenance, RubricCriterion, RubricLevel, TaskVersion, TestCase
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, load_profile
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = load_profile(REPO_ROOT / "subjects" / "cs_intro_c.yaml")
AUTHOR = UserId("usr_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

REFERENCE = """#include <stdio.h>
int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a + b);
    return 0;
}
"""

# **参照解答とは書き方が違うが、振る舞いは同じ。** 突き合わせでは落ちるが
# テストケースは通る ── 比べるべきは字面ではないことの実例。
DIFFERENT_BUT_CORRECT = """#include <stdio.h>
int main(void) {
    int x = 0, y = 0;
    if (scanf("%d", &x) != 1) return 1;
    if (scanf("%d", &y) != 1) return 1;
    int sum = x;
    sum += y;
    printf("%d\\n", sum);
    return 0;
}
"""

WRONG = """#include <stdio.h>
int main(void) {
    printf("0\\n");
    return 0;
}
"""


def _attempt(solution: str, understanding: str = "2 つの整数を読んで和を出す") -> str:
    return json.dumps(
        {"understanding": understanding, "solution": solution}, ensure_ascii=False
    )


def _task(reference: str = REFERENCE, cases: bool = True) -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="## 2 数の和 ##\n\n2 つの整数を読み、その和を出力しなさい。",
        reference_solution=reference,
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "4" * 32),
                code="correctness",
                title="正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
                evaluator_id="code_test_runner",
            ),
        ),
        test_cases=(
            (
                TestCase(
                    name="c1",
                    evaluator_id="code_test_runner",
                    payload={"input": "2 3\n", "expected": "5\n"},
                ),
                TestCase(
                    name="c2",
                    evaluator_id="code_test_runner",
                    payload={"input": "10 1\n", "expected": "11\n"},
                ),
            )
            if cases
            else ()
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=AUTHOR, generated_by="drafter"),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _checker(payload: str) -> tuple[SolvabilityChecker, ScriptedProvider]:
    provider = ScriptedProvider([payload])
    verifier = TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE)
    return (
        SolvabilityChecker(verifier, LlmGateway(provider), solver_model="solver-stub"),
        provider,
    )


def test_the_reference_solution_is_never_shown_to_the_solver() -> None:
    """**入れたら何も測っていない。**"""
    checker, provider = _checker(_attempt(DIFFERENT_BUT_CORRECT))
    checker.check(_task())

    sent = "\n".join(m.content for m in provider.calls[0].messages)
    assert "2 つの整数を読み" in sent, "課題文が渡っていない"
    assert "scanf(\"%d %d\"" not in sent, "参照解答がプロンプトに入っている"


@needs_c_compiler
def test_a_different_but_correct_solution_counts_as_solved() -> None:
    """**参照解答と突き合わせない。** 正しいプログラムは何通りもある。"""
    checker, _ = _checker(_attempt(DIFFERENT_BUT_CORRECT))
    report = checker.check(_task())

    assert report.outcome is SolvabilityOutcome.SOLVED
    assert report.solved


@needs_c_compiler
def test_a_wrong_solution_is_reported_but_not_a_rejection() -> None:
    """落ちた原因が「曖昧」か「難しい」かは機械には分けられない（P5）。"""
    checker, _ = _checker(_attempt(WRONG, understanding="0 と出力する課題だと読んだ"))
    report = checker.check(_task())

    assert report.outcome is SolvabilityOutcome.UNSOLVED
    text = report.summary()
    assert "却下の理由ではありません" in text
    # 課題文の曖昧さは読み違えとして現れる。残さないと原因が追えない。
    assert "0 と出力する課題だと読んだ" in text


def test_a_model_that_does_not_answer_is_not_an_unsolvable_task() -> None:
    """**落ちたのは課題ではなく検査の側である。**"""
    verifier = TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE)
    checker = SolvabilityChecker(
        verifier, LlmGateway(ScriptedProvider([])), solver_model="solver-stub"
    )
    report = checker.check(_task())

    assert report.outcome is SolvabilityOutcome.NOT_RUN
    assert not report.solved
    assert "検査していません" in report.summary()


def test_without_test_cases_nothing_can_be_judged() -> None:
    checker, _ = _checker(_attempt(DIFFERENT_BUT_CORRECT))
    report = checker.check(_task(cases=False))
    assert report.outcome is SolvabilityOutcome.NOT_RUN


# -- レビューに渡す束 --------------------------------------------------------


@needs_c_compiler
def test_the_packet_puts_the_gates_and_solvability_in_front_of_the_instructor() -> None:
    """検査は材料であって判定ではない。**決めるのは教員**（P5）。"""
    task = _task()
    verifier = TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE, mutation_limit=8)
    checker, _ = _checker(_attempt(DIFFERENT_BUT_CORRECT))

    packet = build_packet(task, verifier.verify(task), checker.check(task))
    text = packet.render()

    assert packet.clean
    assert "門 2 つを通っています" in text
    assert "solver-stub" in text
    assert "承認するかどうかは教員が決めます" in text


@needs_c_compiler
def test_an_unsolved_task_is_not_clean_but_is_still_shown() -> None:
    task = _task()
    verifier = TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE, mutation_limit=8)
    checker, _ = _checker(_attempt(WRONG))

    packet = build_packet(task, verifier.verify(task), checker.check(task))
    assert not packet.clean
    # 自動で捨てない。捨てると難しい良問から先に消える。
    assert "解けませんでした" in packet.render()
