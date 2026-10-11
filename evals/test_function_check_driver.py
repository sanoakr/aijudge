"""関数の検査（2026-10-11）を、実際のパイプラインで採点する。

課題は「`int sqsum(int n)`（1² + … + n²）を定義して使う」。入出力 2 件と、
検査用の `main()` で `sqsum()` を直接呼ぶ検査 1 件（`c_function_check` あて）を
持たせ、関数 0.1 → 正しさ 0.45 → インデント 0.45 の AND で組む（prog2 第 9 回以降の
形）。確かめたいのは:

- 関数を定義して使う解は満点
- 関数を使わず `main()` に全部書いた解は、出力が合っていても 0 点（関数が無い）
- 型が違えば 0 点（1 本の翻訳単位でコンパイルするので、コンパイルで落ちる）
- 名前だけの空の関数は、検査の出力が合わずに 0 点
- 検査用の `main()` は入出力のケースには使われない
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Aggregation,
    Artifact,
    ArtifactKind,
    ArtifactRole,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core import (
    TestCase as CaseModel,
)
from aijudge_core.ids import ArtifactId, CourseId, CriterionId, SubmissionId, TaskVersionId, UserId
from aijudge_eval_code_test_runner import DRIVER_KEY, FUNCTION_CHECK_ID
from aijudge_grading import GradingPipeline, default_registry, load_profile, protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "prog2-2025-ex06-p3"
PROFILE_PATH = REPO_ROOT / "subjects" / "cs_lang_c_intro.yaml"
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

REFERENCE = """#include <stdio.h>

int sqsum(int n)
{
    int sum = 0;
    for (int i = 1; i <= n; i++) {
        sum += i * i;
    }
    return sum;
}

int main(void)
{
    int n;
    scanf("%d", &n);
    printf("%d\\n", sqsum(n));
    return 0;
}
"""

# 関数を使わずに main() へ全部書いた解。出力は参照解答と同じ。
INLINE = """#include <stdio.h>

int main(void)
{
    int n, sum = 0;
    scanf("%d", &n);
    for (int i = 1; i <= n; i++) {
        sum += i * i;
    }
    printf("%d\\n", sum);
    return 0;
}
"""

DRIVER = """#include <stdio.h>

int sqsum(int n);

int main(void)
{
    printf("%d %d %d\\n", sqsum(0), sqsum(3), sqsum(10));
    return 0;
}
"""

FUNCTION = RubricCriterion(
    id=CriterionId("crt_" + "7" * 32),
    code="function",
    title="関数の定義",
    description="指定の関数を定義しているか。",
    weight=0.1,
    levels=(
        RubricLevel(level=0, label="未達", descriptor="定義していない", score_ratio=0.0),
        RubricLevel(level=1, label="達成", descriptor="定義している", score_ratio=1.0),
    ),
    evaluator_id=FUNCTION_CHECK_ID,
)

INDENTATION = RubricCriterion(
    id=CriterionId("crt_" + "8" * 32),
    code="indentation",
    title="インデント",
    description="入れ子の深さどおりに字下げされているか。",
    weight=0.45,
    levels=(
        RubricLevel(level=0, label="未達", descriptor="揃っていない", score_ratio=0.0),
        RubricLevel(level=1, label="一部", descriptor="一部崩れている", score_ratio=0.5),
        RubricLevel(level=2, label="達成", descriptor="揃っている", score_ratio=1.0),
    ),
    evaluator_id="c_indent_check",
)


def _io(name: str, given: str, expected: str) -> CaseModel:
    return CaseModel(
        name=name,
        evaluator_id="code_test_runner",
        payload={"input": given, "expected": expected},
    )


@pytest.fixture(scope="module")
def task_version():
    imported = sharif_judge.import_problem(
        FIXTURE,
        course_id=CourseId("crs_" + "0" * 32),
        subject_profile="cs_lang_c_intro",
        authored_by=UserId(new_id("usr")),
    )
    correctness = imported.criteria[0].model_copy(
        update={"weight": 0.45, "evaluator_id": "code_test_runner"}
    )
    check = CaseModel(
        name="function1",
        evaluator_id=FUNCTION_CHECK_ID,
        payload={"input": "", "expected": "0 14 385\n", DRIVER_KEY: DRIVER},
    )
    return imported.model_copy(
        update={
            "criteria": (FUNCTION, correctness, INDENTATION),
            "aggregation": Aggregation.AND,
            "reference_solution": REFERENCE,
            "test_cases": (_io("case1", "2\n", "5\n"), _io("case2", "4\n", "30\n"), check),
        }
    )


def _grade(source: str, task_version):  # type: ignore[no-untyped-def]
    submission_id = SubmissionId(new_id("sub"))
    payload = source.encode()
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        filename="sqsum.c",
        storage_key=f"memory://{submission_id}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=NOW,
    )
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    registry = default_registry()
    pipeline = GradingPipeline(registry, load_profile(PROFILE_PATH, registry))
    return pipeline.run(
        task_version,
        submission,
        lambda _artifact: payload,
        aggregation=task_version.aggregation or Aggregation.OR,
    )


def _result(run, evaluator_id: str):  # type: ignore[no-untyped-def]
    return next(r for r in run.evaluator_results if r.evaluator_id == evaluator_id)


def _score_of(run, criterion_id):  # type: ignore[no-untyped-def]
    return next(s for s in run.criterion_scores if s.criterion_id == criterion_id)


def test_defining_and_using_the_function_scores_full_marks(task_version) -> None:
    run = _grade(REFERENCE, task_version)
    assert run.score_ratio == pytest.approx(1.0)
    # 入出力のケースは学生の main() で、検査は検査用の main() で走っている。
    runner = _result(run, "code_test_runner").raw_output
    assert [case["name"] for case in runner["cases"]] == ["case1", "case2"]
    check = _result(run, FUNCTION_CHECK_ID).raw_output
    assert [case["name"] for case in check["cases"]] == ["function1"]
    assert "関数の検査 1 件に通りました" in _score_of(run, FUNCTION.id).rationale


def test_writing_everything_in_main_scores_zero(task_version) -> None:
    """出力は合っていても、関数が無ければ 0 点。AND なので後ろの観点は評価しない。"""
    run = _grade(INLINE, task_version)
    assert run.score_ratio == pytest.approx(0.0)
    correctness = task_version.criteria[1].id
    assert set(run.skipped_criteria) == {correctness, INDENTATION.id}
    assert "関数 sqsum() が定義されていません" in _score_of(run, FUNCTION.id).rationale


def test_a_different_signature_scores_zero(task_version) -> None:
    source = REFERENCE.replace("int sqsum(int n)", "double sqsum(double n)").replace(
        "int sum = 0;", "double sum = 0;"
    )
    source = source.replace('printf("%d\\n", sqsum(n));', 'printf("%d\\n", (int)sqsum(n));')
    run = _grade(source, task_version)
    assert run.score_ratio == pytest.approx(0.0)
    assert "型が課題の指定と違います" in _score_of(run, FUNCTION.id).rationale


def test_an_empty_function_with_the_right_name_scores_zero(task_version) -> None:
    source = INLINE.replace(
        "int main(void)", "int sqsum(int n)\n{\n    return n;\n}\n\nint main(void)"
    )
    run = _grade(source, task_version)
    assert run.score_ratio == pytest.approx(0.0)
    assert "関数の結果が期待と違いました" in _score_of(run, FUNCTION.id).rationale


def test_a_right_function_with_a_wrong_main_keeps_the_function_share(task_version) -> None:
    """関数は正しく main() の出力が全部違う解は、関数の 1 割だけ（2026-10-11 の決定）。"""
    source = REFERENCE.replace('printf("%d\\n", sqsum(n));', 'printf("%d\\n", 0);')
    run = _grade(source, task_version)
    assert run.score_ratio == pytest.approx(0.1)
    assert INDENTATION.id in run.skipped_criteria


def test_a_submission_that_does_not_compile_fails_the_function_check(task_version) -> None:
    run = _grade("int main(void) { return }\n", task_version)
    assert run.score_ratio == pytest.approx(0.0)
    assert "コンパイルに失敗" in _score_of(run, FUNCTION.id).rationale


def test_the_console_does_not_edit_the_checks_as_io_cases() -> None:
    """入出力の形を名乗らない ── 名乗ると、入出力の編集欄で保存するたびに検査用の
    ソースが落ちる。知らない形は画面が「編集できません」と出し、保存で持ち越す。"""
    registry = default_registry()
    assert protocol.test_case_shape(registry.get(FUNCTION_CHECK_ID)) == "function"
    assert protocol.test_case_shape(registry.get("code_test_runner")) == "io"


def test_a_driver_without_prototypes_still_names_the_missing_function(task_version) -> None:
    """検査用のソースがプロトタイプを書かない形（`const` の有無を問わないため）でも、
    関数が無ければ 0 点で、理由に関数の名前が出る。"""
    driver = DRIVER.replace("int sqsum(int n);\n", "")
    (io1, io2, check) = task_version.test_cases
    bare = check.model_copy(update={"payload": {**check.payload, DRIVER_KEY: driver}})
    version = task_version.model_copy(update={"test_cases": (io1, io2, bare)})
    run = _grade(INLINE, version)
    assert run.score_ratio == pytest.approx(0.0)
    assert "関数 sqsum() が定義されていません" in _score_of(run, FUNCTION.id).rationale
    assert _grade(REFERENCE, version).score_ratio == pytest.approx(1.0)
