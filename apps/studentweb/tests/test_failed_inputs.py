"""落ちたテストケースの入力を学習者に見せる（2026-10-18）。

「不一致: case1」だけでは、非公開のケースでどの入力のときに間違えたのかが分からず、
直しようがない。固定するのは 4 つ。

入力だけ        期待出力は見せない（答えそのものになる）
落ちたものだけ  通ったケースは出さない。観点ごとに最初の 3 件まで
設定に従う      すぐ / 締切後 / 見せない。締切後で締切が無ければ見せない
渡されなければ  課題を知らない呼び出しでは見せない（既定で漏らさない）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorResult,
    FailedInputs,
    GradingContext,
    GradingRun,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
    new_id,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_studentweb.visibility import MAX_FAILED_INPUTS, build_result_view

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
CORRECTNESS = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
RESULT = EvaluatorResultId("evr_" + "6" * 32)
SECRET_EXPECTED = "THE-EXPECTED-OUTPUT"


def _version(cases: int = 5) -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "4" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="問題文",
        criteria=(
            RubricCriterion(
                id=CORRECTNESS,
                code="correctness",
                title="出力の正しさ",
                description="テスト実行で判定する",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
                evaluator_id="code_test_runner",
            ),
        ),
        test_cases=tuple(
            TestCase(
                name=f"case{i}",
                evaluator_id="code_test_runner",
                payload={"input": f"input-{i}\n", "expected": SECRET_EXPECTED},
            )
            for i in range(1, cases + 1)
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "5" * 32)),
        created_at=NOW,
    )


def _run(failed: set[str], cases: int = 5) -> GradingRun:
    entries = [
        {
            "name": f"case{i}",
            "hidden": True,
            "passed": f"case{i}" not in failed,
            "expected": [SECRET_EXPECTED],
            "actual": ["?"],
        }
        for i in range(1, cases + 1)
    ]
    return GradingRun(
        id=GradingRunId(new_id("grn")),
        submission_id=SubmissionId(new_id("sub")),
        context=GradingContext(
            task_version_id=VERSION,
            subject_profile="cs_lang_c_intro",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        evaluator_results=(
            EvaluatorResult(
                id=RESULT,
                evaluator_id="code_test_runner",
                kind=EvaluatorKind.DETERMINISTIC,
                raw_output={"cases": entries},
            ),
        ),
        criterion_scores=(
            CriterionScore(
                id=CriterionScoreId(new_id("cs")),
                criterion_id=CORRECTNESS,
                evaluator_result_id=RESULT,
                kind=EvaluatorKind.DETERMINISTIC,
                level=0,
                score_ratio=0.0,
                weight=1.0,
                confidence=1.0,
                conclusive=True,
                rationale="不一致",
            ),
        ),
        score_ratio=0.0,
        confidence=1.0,
        routing=Routing.REVIEW_REQUIRED,
        created_at=NOW,
    )


def _shown(run: GradingRun, version: TaskVersion | None = None, **kwargs) -> tuple:
    view = build_result_view(run, version or _version(), None, now=NOW, **kwargs)
    return view.criteria[0].failed_inputs


def test_only_the_inputs_of_failed_cases_are_shown() -> None:
    shown = _shown(_run({"case2", "case4"}), failed_inputs=FailedInputs.IMMEDIATELY)
    assert [(case.name, case.input) for case in shown] == [
        ("case2", "input-2\n"),
        ("case4", "input-4\n"),
    ]
    # **期待出力は持たない。** 型にも無い。
    assert all(SECRET_EXPECTED not in repr(case) for case in shown)


def test_only_the_first_few_are_shown() -> None:
    view = build_result_view(
        _run({"case1", "case2", "case3", "case4", "case5"}),
        _version(),
        None,
        now=NOW,
        failed_inputs=FailedInputs.IMMEDIATELY,
    )
    row = view.criteria[0]
    assert len(row.failed_inputs) == MAX_FAILED_INPUTS
    assert row.more_failed


def test_never_shows_nothing() -> None:
    assert _shown(_run({"case1"}), failed_inputs=FailedInputs.NEVER) == ()


def test_without_the_setting_nothing_is_shown() -> None:
    """課題を知らない呼び出し（一覧の集計など）からは漏らさない。"""
    assert _shown(_run({"case1"})) == ()


@pytest.mark.parametrize(
    ("deadline", "shown"),
    [
        (NOW + timedelta(minutes=1), False),
        (NOW - timedelta(minutes=1), True),
        # 過ぎる時刻が無い。「すぐ」に倒すと、締切後にしたかった設定が黙って効かない。
        (None, False),
    ],
)
def test_after_deadline_waits_for_the_deadline(deadline, shown: bool) -> None:
    result = _shown(_run({"case1"}), failed_inputs=FailedInputs.AFTER_DEADLINE, deadline=deadline)
    assert bool(result) is shown


def test_a_long_input_is_cut() -> None:
    version = _version(1)
    long_case = version.test_cases[0].model_copy(
        update={"payload": {"input": "x" * 5000, "expected": SECRET_EXPECTED}}
    )
    version = version.model_copy(update={"test_cases": (long_case,)})
    [case] = _shown(_run({"case1"}, cases=1), version, failed_inputs=FailedInputs.IMMEDIATELY)
    assert case.truncated
    assert len(case.input) < 5000
