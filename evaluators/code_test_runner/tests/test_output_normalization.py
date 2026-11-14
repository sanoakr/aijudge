"""出力比較の正規化（`normalize_output`）が緩めすぎないこと（#703）。

約束は docstring のとおり「行末の空白と末尾の空行だけを無視する」。**行頭の空白は
区別する。** ピラミッドや右寄せの表のように、字下げそのものが課題である問題が
あるから。以前は直接のテストが無く、`rstrip` を `strip` に変えても全件が通った
（#703 の変異）── 字下げを 1 つも出さない提出が正解になる退行を検出できなかった。

`normalize_output` はコンソールの差分表示と `task_verifier` も使うので、ここが
緩むと表示も一緒にずれて、食い違いが目に見えない。

実行は決まった結果を返す作業域で置き換える（`test_exit_expectation.py` と同じ）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_core import TestCase
from aijudge_eval_code_test_runner import CodeTestRunner, normalize_output, resolve_language
from aijudge_sandbox import ExecRequest, ExecResult, Isolation, Limits

LIMITS = Limits(cpu_seconds=1, wall_seconds=1.0)


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        # 行頭の空白（ピラミッド）。
        ("  *\n ***\n*****\n", "*\n***\n*****\n"),
        # 行頭の空白の数だけが違う。
        ("  *\n", " *\n"),
        # 行の途中の空白の数（「2  2」と「2 2」は別の書式）。
        ("2  2\n", "2 2\n"),
        # 先頭の空行（末尾の空行とは違い、出力の一部として扱う）。
        ("\nresult\n", "result\n"),
        # 行頭のタブと空白。
        ("\tx\n", "    x\n"),
    ],
)
def test_differences_that_are_part_of_the_format_are_kept(expected: str, actual: str) -> None:
    assert normalize_output(expected) != normalize_output(actual)


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        # 行末の空白。
        ("  *\n ***\n", "  *   \n ***\t\n"),
        # 末尾の空行。
        ("1\n2\n", "1\n2\n\n\n"),
        # 末尾の改行の有無。
        ("1\n2\n", "1\n2"),
        # CRLF。
        ("  *\n ***\n", "  *\r\n ***\r\n"),
    ],
)
def test_only_trailing_whitespace_and_blank_lines_are_ignored(expected: str, actual: str) -> None:
    assert normalize_output(expected) == normalize_output(actual)


def test_leading_whitespace_survives_normalisation() -> None:
    """比較の両側と、コンソールの差分表示が見る値そのもの。"""
    assert normalize_output("  *\n ***\n") == ["  *", " ***"]


class _Workspace:
    def __init__(self, result: ExecResult) -> None:
        self.path = Path("/nonexistent")
        self._result = result

    def write(self, name: str, content: bytes | str) -> Path:
        return self.path / name

    def run(self, request: ExecRequest) -> ExecResult:
        return self._result


def test_output_differing_only_in_leading_whitespace_is_a_mismatch() -> None:
    """採点の経路でも、字下げを出さない提出は不正解になる。"""
    case = TestCase(
        name="pyramid",
        evaluator_id="code_test_runner",
        payload={"input": "3\n", "expected": "  *\n ***\n*****\n"},
    )
    flat = ExecResult(exit_code=0, stdout="*\n***\n*****\n", isolation=Isolation.CONTAINER)
    [entry] = CodeTestRunner()._run_cases([case], _Workspace(flat), resolve_language({}), LIMITS)
    assert entry["passed"] is False
    assert entry["reason"] == "output mismatch"
    assert entry["expected"] == ["  *", " ***", "*****"]


def test_output_differing_only_in_trailing_whitespace_passes() -> None:
    """上の試験が「何でも不正解にする」ことで通っていないことの確かめ。"""
    case = TestCase(
        name="pyramid",
        evaluator_id="code_test_runner",
        payload={"input": "3\n", "expected": "  *\n ***\n*****\n"},
    )
    padded = ExecResult(
        exit_code=0, stdout="  *  \r\n ***\r\n*****\r\n\r\n", isolation=Isolation.CONTAINER
    )
    [entry] = CodeTestRunner()._run_cases([case], _Workspace(padded), resolve_language({}), LIMITS)
    assert entry["passed"] is True
