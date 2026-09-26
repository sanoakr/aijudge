"""確定の画面に、入出力セットとの突き合わせを出す。

固定したいのは 5 つ。

違う行を揃える   1 行抜けた出力で、以降の全行を「違う」にしない。
入力を添える     記録に入力は無い。課題の入出力セットから名前で引く。
無いものは無い   時間切れのケースには出力が記録されていない。空の出力と同じ顔にしない。
結びは ID で     同じ評価器を 2 つの観点に割り当てても、観点ごとの結果になる。
切る             無限ループで出し続けた出力を、そのまま画面に描かない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    GradingRun,
    Provenance,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_reviewconsole.app import TEMPLATES
from aijudge_reviewconsole.io_results import MAX_SHOWN_LINES, io_results

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
CORRECT = CriterionId("crt_" + "1" * 32)
STYLE = CriterionId("crt_" + "2" * 32)
RESULT = "evr_" + "3" * 32
OTHER_RESULT = "evr_" + "4" * 32
RUNNER = "code_test_runner"


def _criterion(criterion_id: CriterionId, code: str) -> RubricCriterion:
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=code,
        description="出力が一致するか。",
        weight=0.5,
        evaluator_id=RUNNER,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="一致しない", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="一致する", score_ratio=1.0),
        ),
    )


def _task() -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "5" * 32),
        task_id=TaskId("tsk_" + "6" * 32),
        version=1,
        subject_profile="cs_lang_c_intro",
        statement="最大・最小・平均",
        criteria=(_criterion(CORRECT, "correct"), _criterion(STYLE, "style")),
        max_score=100.0,
        test_cases=(
            TestCase(name="case1", evaluator_id=RUNNER, payload={"input": "1 2\n", "expected": ""}),
            TestCase(name="case2", evaluator_id=RUNNER, payload={"input": "3\n", "expected": ""}),
        ),
        provenance=Provenance(authored_by=UserId("usr_" + "7" * 32)),
        created_at=NOW,
    )


def _score(criterion_id: CriterionId, result_id: str) -> dict:
    return {
        "id": "cs_" + criterion_id[-32:],
        "criterion_id": str(criterion_id),
        "evaluator_result_id": result_id,
        "kind": "deterministic",
        "level": 0,
        "score_ratio": 0.0,
        "weight": 0.5,
        "confidence": 1.0,
        "conclusive": True,
        "evidence": [
            {
                "artifact_id": "art_" + "8" * 32,
                "artifact_content_hash": "x",
                "span": {"kind": "whole"},
            }
        ],
        "rationale": "5 件中 3 件が一致しました。",
    }


def _run(*results: tuple[str, dict], scores: tuple[dict, ...] | None = None) -> GradingRun:
    return GradingRun.model_validate(
        {
            "id": "grn_" + "9" * 32,
            "submission_id": "sub_" + "a" * 32,
            "context": {
                "task_version_id": "tsv_" + "5" * 32,
                "subject_profile": "cs_lang_c_intro",
                "rubric_version": "r@1",
                "input_hash": "sha256:x",
                "pipeline_version": "1",
            },
            "evaluator_results": [
                {"id": rid, "evaluator_id": RUNNER, "kind": "deterministic", "raw_output": raw}
                for rid, raw in results
            ],
            "criterion_scores": list(scores or (_score(CORRECT, RESULT),)),
            "score_ratio": 0.0,
            "confidence": 1.0,
            "routing": "review_required",
            "created_at": "2026-09-26T00:00:00Z",
        }
    )


def _mismatch(name: str, expected: list[str], actual: list[str]) -> dict:
    return {
        "name": name,
        "weight": 1.0,
        "hidden": True,
        "passed": False,
        "exit_code": 0,
        "expected": expected,
        "actual": actual,
        "reason": "output mismatch",
    }


def test_a_missing_line_does_not_mark_every_later_line_as_different() -> None:
    """**添字で揃えない。** 1 行抜けただけなら、違うのはその 1 行である。"""
    run = _run((RESULT, {"cases": [_mismatch("case1", ["a", "b", "c", "d"], ["a", "c", "d"])]}))

    (case,) = io_results(_task(), run)[CORRECT].cases

    assert [(line.expected, line.actual, line.same) for line in case.lines] == [
        ("a", "a", True),
        ("b", None, False),
        ("c", "c", True),
        ("d", "d", True),
    ]
    assert case.reason == "出力が違う"
    assert case.hidden


def test_the_input_is_taken_from_the_task_by_name() -> None:
    """評価器の記録に入力は無い。**課題の入出力セットから引く。**"""
    run = _run((RESULT, {"cases": [_mismatch("case2", ["x"], ["y"])]}))

    (case,) = io_results(_task(), run)[CORRECT].cases

    assert case.input == "3\n"


def test_a_timeout_is_not_shown_as_empty_output() -> None:
    """時間切れのケースには実際の出力が無い。**「何も出さなかった」と読ませない。**"""
    run = _run(
        (
            RESULT,
            {"cases": [{"name": "case1", "weight": 1.0, "passed": False, "reason": "timeout"}]},
        )
    )

    (case,) = io_results(_task(), run)[CORRECT].cases

    assert not case.compared
    assert case.reason == "時間切れ"


def test_each_criterion_gets_its_own_evaluator_result() -> None:
    """**評価器の名前で結ばない。** 同じ評価器を 2 つの観点に割り当てた課題がある。"""
    run = _run(
        (RESULT, {"cases": [_mismatch("case1", ["1"], ["2"])]}),
        (OTHER_RESULT, {"cases": [_mismatch("case2", ["3"], ["4"])]}),
        scores=(_score(CORRECT, RESULT), _score(STYLE, OTHER_RESULT)),
    )

    results = io_results(_task(), run)

    assert [case.name for case in results[CORRECT].cases] == ["case1"]
    assert [case.name for case in results[STYLE].cases] == ["case2"]


def test_a_criterion_not_judged_by_io_cases_is_left_out() -> None:
    run = _run((RESULT, {"verdict": {"level": 1}}))

    assert io_results(_task(), run) == {}


def test_a_compile_error_is_shown_without_cases() -> None:
    run = _run((RESULT, {"compile_error": "main.c:3: error: expected ';'", "timed_out": False}))

    result = io_results(_task(), run)[CORRECT]

    assert result.compile_error == "main.c:3: error: expected ';'"
    assert result.cases == ()


def test_endless_output_is_cut() -> None:
    """記録は 1 MiB まである。無限ループの出力をそのまま描くと画面が止まる。"""
    flood = ["y"] * (MAX_SHOWN_LINES * 5)
    run = _run((RESULT, {"cases": [_mismatch("case1", ["n"], flood)]}))

    (case,) = io_results(_task(), run)[CORRECT].cases

    assert len(case.lines) <= MAX_SHOWN_LINES
    assert case.lines_truncated


def _render(run: GradingRun) -> str:
    return TEMPLATES.env.get_template("_io_result.html").render(
        io=io_results(_task(), run)[CORRECT]
    )


def test_the_page_puts_expected_and_actual_side_by_side() -> None:
    run = _run(
        (
            RESULT,
            {
                "cases": [
                    _mismatch("case1", ["2 2 2.000"], ["2 2 2.00"]),
                    {"name": "case2", "weight": 1.0, "passed": True, "expected": [], "actual": []},
                ]
            },
        )
    )

    html = _render(run)

    assert "1 / 2 件一致" in html
    assert "2 2 2.000" in html
    assert "2 2 2.00" in html
    assert "期待する出力" in html and "実際の出力" in html
    assert 'class="differs"' in html
    # 落ちたケースがあれば開いておく。見るべきものはそちらである。
    assert '<details class="io-result" open' in html


def test_all_cases_passing_stays_folded() -> None:
    run = _run(
        (
            RESULT,
            {
                "cases": [
                    {"name": "case1", "weight": 1.0, "passed": True, "expected": [], "actual": []}
                ]
            },
        )
    )

    html = _render(run)

    assert "1 / 1 件一致" in html
    assert '<details class="io-result" >' in html
    assert "期待する出力" not in html


def test_the_output_is_escaped() -> None:
    """**学習者の出力をそのまま HTML に流さない。**"""
    run = _run((RESULT, {"cases": [_mismatch("case1", ["ok"], ["<script>alert(1)</script>"])]}))

    html = _render(run)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
