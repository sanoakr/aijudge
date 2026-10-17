"""テストケースごとの終了コードの期待（2026-10-18）。

C の規約では 0 が成功、`EXIT_FAILURE` が失敗を表す。「入力が不正なら何も出力
せずに終了する」ケースで `return 1;` と書くのは慣習どおりなのに、以前は 0 以外を
一律に不正解にしていた。ケースが `exit_code` に `nonzero` か `any` を書けば、
規約どおりの解答を正解にできる。

**どの期待でも、シグナルによる終了と時間切れは不正解のまま**であることも固定する。
緩めたのは「正常に終了し、終了コードが 0 以外」だけである。

実行はサンドボックスを使わず、決まった結果を返す作業域で置き換える ── 確かめたい
のは判定の規則で、コンパイラや隔離ではない（実物は `apps/admin/tests/
test_exit_code_with_compiler.py` が走らせる）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_core import TestCase
from aijudge_eval_code_test_runner import (
    EXIT_ANY,
    EXIT_NONZERO,
    EXIT_ZERO,
    CodeTestRunner,
    exit_expectation,
    resolve_language,
)
from aijudge_sandbox import ExecRequest, ExecResult, Isolation, Limits

LIMITS = Limits(
    cpu_seconds=1,
    wall_seconds=1.0,
    memory_bytes=64 * 1024 * 1024,
    processes=4,
    output_bytes=1024,
)


class _Workspace:
    """実行のたびに、渡された結果を順に返す作業域。"""

    def __init__(self, results: list[ExecResult]) -> None:
        self.path = Path("/nonexistent")
        self._results = list(results)

    def write(self, name: str, content: bytes | str) -> Path:
        return self.path / name

    def run(self, request: ExecRequest) -> ExecResult:
        return self._results.pop(0)


def _result(code: int, stdout: str = "", **extra) -> ExecResult:
    return ExecResult(exit_code=code, stdout=stdout, isolation=Isolation.CONTAINER, **extra)


def _case(expected: str, exit_code: str | None = None) -> TestCase:
    payload: dict[str, object] = {"input": "", "expected": expected}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    return TestCase(name="c1", evaluator_id="code_test_runner", payload=payload)


def _run(case: TestCase, result: ExecResult) -> dict[str, object]:
    runner = CodeTestRunner()
    [entry] = runner._run_cases([case], _Workspace([result]), resolve_language({}), LIMITS)
    return entry


def test_by_default_a_nonzero_exit_fails_even_with_the_right_output() -> None:
    """書かなければ従来どおり。既存の課題の採点は変わらない。"""
    entry = _run(_case("ok\n"), _result(1, "ok\n"))
    assert entry["passed"] is False
    assert entry["reason"] == "nonzero exit"
    assert "expected_exit" not in entry


def test_nonzero_accepts_an_error_exit_with_the_right_output() -> None:
    """**この変更の目的。** 何も出力せず `return 1;` で終える解答を正解にする。"""
    entry = _run(_case("", EXIT_NONZERO), _result(1, ""))
    assert entry["passed"] is True
    assert entry["expected_exit"] == EXIT_NONZERO


def test_nonzero_refuses_a_zero_exit() -> None:
    """エラーを成功として返した解答は通さない。理由を出力の不一致と言わない。"""
    entry = _run(_case("", EXIT_NONZERO), _result(0, ""))
    assert entry["passed"] is False
    assert entry["reason"] == "zero exit"


def test_nonzero_still_compares_the_output() -> None:
    entry = _run(_case("", EXIT_NONZERO), _result(1, "error\n"))
    assert entry["passed"] is False
    assert entry["reason"] == "output mismatch"


@pytest.mark.parametrize("code", [0, 1, 3, 255])
def test_any_accepts_every_normal_exit(code: int) -> None:
    entry = _run(_case("ok\n", EXIT_ANY), _result(code, "ok\n"))
    assert entry["passed"] is True


@pytest.mark.parametrize("expectation", [EXIT_ZERO, EXIT_NONZERO, EXIT_ANY])
def test_a_crash_fails_whatever_the_case_expects(expectation: str) -> None:
    """**異常終了は緩めない。** 出力が出きったあとの segfault でも不正解。"""
    entry = _run(_case("ok\n", expectation), _result(139, "ok\n", signal_name="SIGSEGV"))
    assert entry["passed"] is False
    assert entry["signal"] == "SIGSEGV"


@pytest.mark.parametrize("expectation", [EXIT_NONZERO, EXIT_ANY])
def test_a_timeout_fails_whatever_the_case_expects(expectation: str) -> None:
    entry = _run(_case("", expectation), _result(124, "", timed_out=True))
    assert entry["passed"] is False
    assert entry["reason"] == "timeout"


def test_an_unknown_expectation_is_read_as_zero() -> None:
    """**知らない値は厳しい側に倒す。** 誤記が採点を甘くしない。"""
    assert exit_expectation({"exit_code": "nonzer0"}) == EXIT_ZERO
    assert exit_expectation({}) == EXIT_ZERO
    entry = _run(_case("ok\n", "whatever"), _result(1, "ok\n"))
    assert entry["passed"] is False


def test_the_rationale_says_why_a_zero_exit_failed() -> None:
    """出力が合っていても落ちるので、「不一致」だけでは出力の側を探させる。"""
    runner = CodeTestRunner()
    failed = [{"name": "bad_input", "reason": "zero exit", "expected": [], "actual": []}]
    text = runner._rationale(1, 2, failed)
    assert "bad_input" in text
    assert "0 以外の終了コード" in text
