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

import contextlib
import difflib
import itertools
from dataclasses import dataclass

from aijudge_core import GradingRun, TaskVersion, TestCase
from aijudge_core.ids import CriterionId
from aijudge_eval_network_test_runner import COMPANION_HOST
from aijudge_eval_network_test_runner import EVALUATOR_ID as NETWORK_EVALUATOR_ID

# 1 ケースで並べる行の上限。**出力は最大 1 MiB まで記録される**（評価器の
# 上限）ので、無限ループで同じ行を出し続けた提出をそのまま描くと画面が
# 止まる。どこから違うかを見るには、先頭の数百行で足りる。
MAX_SHOWN_LINES = 200
# 入力として見せる文字数の上限。同じ理由で、課題の入力が大きくても切る。
MAX_INPUT_CHARS = 4000
# 標準エラーの上限。評価器の側でも 2000 文字で切ってある。
MAX_STDERR_CHARS = 2000

# クライアント・サーバの課題を採点する評価器（2026-10-01）。**比べ方が違う** ──
# 行を揃えて比べるのではなく、期待する断片が出力に含まれるかを見る。記録の形も
# 違うので（`expected`/`actual` が無く、`submission_stdout`・`missing` がある）、
# 入出力の形で読むと「出力は記録されていません」と誤って出ていた。
NETWORK_TEST_RUNNER = NETWORK_EVALUATOR_ID
# 入力の `{host}` を埋める値。**評価器の定数を読む**（書き写すと、評価器だけ変えた日に
# 採点で渡していない宛先が画面に出る）。
NETWORK_HOST = COMPANION_HOST


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
class Needle:
    """期待する断片 1 つと、出力に見つかったか。出力が記録されていなければ None。"""

    text: str
    found: bool | None


@dataclass(frozen=True)
class NetworkCase:
    """クライアント・サーバの 1 ケース（`network_test_runner`）。"""

    name: str
    passed: bool
    reason: str | None
    # 落ちた理由の詳しい説明（待ち受けなかったときの、何がどのポートで、など）。
    detail: str | None
    hidden: bool
    # 提出物の役割（`client` / `server`）と、伴走プロセスとのやりとりに使うポート。
    role: str
    port: int | None
    # 提出物と伴走プロセスへの入力。**`{host}`・`{port}` は埋めて見せる** ── 採点で
    # 渡したのは埋めた値で、記号のまま見せると何を入れたのか読めない。
    input: str | None
    companion_input: str | None
    expected: tuple[Needle, ...]
    companion_expected: tuple[Needle, ...]
    # 採点のとき記録した出力（評価器が 2000 文字で切っている）。
    submission_stdout: str | None
    companion_stdout: str | None
    # 待ち受けなかったとき、待たれた側（背景のプロセス）の標準エラー出力。
    background_stderr: str | None
    # 出力が記録されているか。時間切れ・待ち受けなしでは記録されない。
    recorded: bool


@dataclass(frozen=True)
class IoResult:
    evaluator_id: str
    cases: tuple[IoCase, ...]
    compile_error: str | None
    # クライアント・サーバの課題のケース。入出力の課題では空。
    network_cases: tuple[NetworkCase, ...] = ()

    @property
    def is_network(self) -> bool:
        return self.evaluator_id == NETWORK_TEST_RUNNER

    @property
    def total(self) -> int:
        return len(self.network_cases) if self.is_network else len(self.cases)

    @property
    def passed(self) -> int:
        cases = self.network_cases if self.is_network else self.cases
        return sum(1 for case in cases if case.passed)

    @property
    def failed(self) -> tuple[IoCase | NetworkCase, ...]:
        cases = self.network_cases if self.is_network else self.cases
        return tuple(case for case in cases if not case.passed)


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
        if result.evaluator_id == NETWORK_TEST_RUNNER:
            if isinstance(cases, list):
                out[score.criterion_id] = _network_result(task, result.evaluator_id, cases)
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


def _network_result(task: TaskVersion, evaluator_id: str, cases: list) -> IoResult:
    """クライアント・サーバの課題の突き合わせ。期待する断片は課題の版から読む。

    評価器が記録するのは**見つからなかった断片**（`missing`）だけなので、期待した
    断片の一覧と非公開かどうかは、採点に使った課題の版のケースから引く。
    """
    defined = {case.name: case for case in task.test_cases if case.evaluator_id == evaluator_id}
    return IoResult(
        evaluator_id=evaluator_id,
        cases=(),
        compile_error=None,
        network_cases=tuple(
            _network_case(entry, defined.get(str(entry.get("name", ""))))
            for entry in cases
            if isinstance(entry, dict)
        ),
    )


def _network_case(entry: dict[str, object], defined: TestCase | None) -> NetworkCase:
    payload = {} if defined is None else dict(defined.payload)
    port = entry.get("port", payload.get("port"))
    port = port if isinstance(port, int) else None
    recorded = isinstance(entry.get("submission_stdout"), str)
    passed = bool(entry.get("passed"))
    return NetworkCase(
        name=str(entry.get("name", "")),
        passed=passed,
        reason=None if passed else _reason(entry),
        detail=_short(entry.get("detail")),
        hidden=bool(defined.hidden) if defined is not None else False,
        role=str(entry.get("role") or payload.get("role") or ""),
        port=port,
        input=_filled(payload.get("input"), port),
        companion_input=_filled(payload.get("companion_input"), port),
        expected=_needles(payload.get("expected_contains"), entry.get("missing"), recorded),
        companion_expected=_needles(
            payload.get("companion_expected_contains"), entry.get("missing_companion"), recorded
        ),
        submission_stdout=_short(entry.get("submission_stdout"), keep_empty=True),
        companion_stdout=_short(entry.get("companion_stdout"), keep_empty=True),
        background_stderr=_short(entry.get("background_stderr")),
        recorded=recorded,
    )


def _needles(wanted: object, missing: object, recorded: bool) -> tuple[Needle, ...]:
    """期待する断片ごとに、見つかったか。**記録が無ければ分からない（None）。**"""
    if not isinstance(wanted, list):
        return ()
    absent = {str(item) for item in missing} if isinstance(missing, list) else set()
    return tuple(
        Needle(text=str(item), found=(str(item) not in absent) if recorded else None)
        for item in wanted
    )


def _filled(value: object, port: int | None) -> str | None:
    """入力の `{host}`・`{port}` を、採点で渡した値に埋める。埋められなければそのまま。"""
    if not isinstance(value, str):
        return None
    text = value
    # 課題の入力に他の波括弧があると埋められない。そのときは書かれたまま見せる。
    with contextlib.suppress(KeyError, IndexError, ValueError):
        text = value.format(host=NETWORK_HOST, port="" if port is None else str(port))
    return text[:MAX_INPUT_CHARS]


def _short(value: object, *, keep_empty: bool = False) -> str | None:
    if not isinstance(value, str) or (not value and not keep_empty):
        return None
    return value[:MAX_STDERR_CHARS]


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
    if reason == "not_listening":
        return "待ち受けが始まらない"
    return reason or "不一致"


__all__ = ["IoCase", "IoLine", "IoResult", "Needle", "NetworkCase", "io_results"]
