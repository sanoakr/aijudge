"""入出力セットとの突き合わせを、確定の画面に出す形にする。

**段階を決める人が、何が違ったのかを見られるようにする。** 評価器
（`code_test_runner`）はケースごとに期待出力と実際の出力を `raw_output` に
残しているが、確定の画面が出していたのは「5 件中 3 件が一致」という根拠の
文だけだった。「書式だけ違う」「最後の 1 行が足りない」「まるで違う」は
どれも同じ文になり、段階を動かすかどうかを判断する材料が画面に無かった。

**採点をやり直さない。** 並べるのは採点のとき記録された出力で、この画面で
提出を走らせ直すことはしない（コンソールは採点しない・ADR 0007）。

**比べるのは正規化した後の行である。** 評価器が比べたのもそれで（行末の
空白と末尾の空行を無視・`normalize_output`）、ここで生の出力を並べると、
一致と判定された行が違って見える。
"""

from __future__ import annotations

import difflib
import itertools
from dataclasses import dataclass

from aijudge_core import GradingRun, TaskVersion
from aijudge_core.ids import CriterionId

# 1 ケースで並べる行の上限。**出力は最大 1 MiB まで記録される**（評価器の
# 上限）ので、無限ループで同じ行を出し続けた提出をそのまま描くと画面が
# 止まる。どこから違うかを見るには、先頭の数百行で足りる。
MAX_SHOWN_LINES = 200
# 入力として見せる文字数の上限。同じ理由で、課題の入力が大きくても切る。
MAX_INPUT_CHARS = 4000
# 標準エラーの上限。評価器の側でも 2000 文字で切ってある。
MAX_STDERR_CHARS = 2000


@dataclass(frozen=True)
class IoLine:
    """期待と実際の 1 行ずつの組。片方が無い行は None（足りない・余分）。"""

    expected: str | None
    actual: str | None
    same: bool


@dataclass(frozen=True)
class IoCase:
    name: str
    passed: bool
    # 落ちた理由を人の言葉で。通ったケースは None。
    reason: str | None
    # 学習者には伏せたケースか（教員には見せる。印だけ付ける）。
    hidden: bool
    input: str | None
    input_truncated: bool
    lines: tuple[IoLine, ...]
    # 並べきれずに省いた行があるか。
    lines_truncated: bool
    # **期待と実際の両方が記録されているか。** 時間切れのケースには実際の
    # 出力が無い ── 空の出力と同じ顔にすると「何も出さなかった」と読まれる。
    compared: bool
    stderr: str | None


@dataclass(frozen=True)
class IoResult:
    evaluator_id: str
    cases: tuple[IoCase, ...]
    compile_error: str | None

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def failed(self) -> tuple[IoCase, ...]:
        return tuple(case for case in self.cases if not case.passed)


def io_results(task: TaskVersion, run: GradingRun) -> dict[CriterionId, IoResult]:
    """観点ごとの、入出力セットとの突き合わせ。入出力セットで判定していない観点は含まない。

    観点と評価器の結果は `CriterionScore.evaluator_result_id` で結ぶ。
    評価器の名前で結ばない ── 同じ評価器を 2 つの観点に割り当てた課題では、
    名前ではどちらの結果か決まらない。
    """
    by_result = {result.id: result for result in run.evaluator_results}
    out: dict[CriterionId, IoResult] = {}
    for score in run.criterion_scores:
        result = by_result.get(score.evaluator_result_id)
        if result is None:
            continue
        raw = result.raw_output
        cases = raw.get("cases")
        compile_error = raw.get("compile_error")
        if not isinstance(cases, list) and not isinstance(compile_error, str):
            continue
        inputs = {
            case.name: case.payload.get("input")
            for case in task.test_cases
            if case.evaluator_id == result.evaluator_id
        }
        out[score.criterion_id] = IoResult(
            evaluator_id=result.evaluator_id,
            cases=tuple(
                _case(entry, inputs)
                for entry in (cases if isinstance(cases, list) else [])
                if isinstance(entry, dict)
            ),
            compile_error=compile_error[:MAX_STDERR_CHARS]
            if isinstance(compile_error, str)
            else None,
        )
    return out


def _case(entry: dict[str, object], inputs: dict[str, object]) -> IoCase:
    name = str(entry.get("name", ""))
    expected = entry.get("expected")
    actual = entry.get("actual")
    compared = _is_lines(expected) and _is_lines(actual)
    lines, truncated = (
        _align(list(expected), list(actual))  # type: ignore[arg-type]
        if compared
        else ((), False)
    )
    raw_input = inputs.get(name)
    text = raw_input if isinstance(raw_input, str) else None
    stderr = entry.get("stderr")
    passed = bool(entry.get("passed"))
    return IoCase(
        name=name,
        passed=passed,
        reason=None if passed else _reason(entry),
        hidden=bool(entry.get("hidden")),
        input=None if text is None else text[:MAX_INPUT_CHARS],
        input_truncated=text is not None and len(text) > MAX_INPUT_CHARS,
        lines=lines,
        lines_truncated=truncated,
        compared=compared,
        stderr=stderr[:MAX_STDERR_CHARS] if isinstance(stderr, str) and stderr else None,
    )


def _is_lines(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(line, str) for line in value)


def _align(expected: list[str], actual: list[str]) -> tuple[tuple[IoLine, ...], bool]:
    """期待と実際を行で揃える。

    **添字で揃えない。** 1 行抜けただけの出力を添字で並べると、以降の
    全行が「違う」になり、どこが違うのかが却って見えなくなる。
    """
    truncated = len(expected) > MAX_SHOWN_LINES or len(actual) > MAX_SHOWN_LINES
    expected, actual = expected[:MAX_SHOWN_LINES], actual[:MAX_SHOWN_LINES]
    rows: list[IoLine] = []
    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    for tag, e_start, e_end, a_start, a_end in matcher.get_opcodes():
        if tag == "equal":
            rows.extend(IoLine(line, line, True) for line in expected[e_start:e_end])
            continue
        rows.extend(
            IoLine(want, got, False)
            for want, got in itertools.zip_longest(expected[e_start:e_end], actual[a_start:a_end])
        )
    return tuple(rows), truncated


def _reason(entry: dict[str, object]) -> str:
    reason = str(entry.get("reason", ""))
    if reason == "timeout":
        return "時間切れ"
    if entry.get("signal"):
        return f"強制終了（{entry['signal']}）"
    if reason == "nonzero exit":
        return f"異常終了（終了コード {entry.get('exit_code')}）"
    if reason == "output mismatch":
        return "出力が違う"
    return reason or "不一致"


__all__ = ["IoCase", "IoLine", "IoResult", "io_results"]
