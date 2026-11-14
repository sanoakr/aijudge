"""崩れの割合がちょうど `partial_max_ratio` のときの段階（#703）。

モジュールの説明は「`partial_max_ratio`（既定 20%）**以下**なら中段」。既存の
試験は閾値から離れた割合（10% と 5%）しか使っておらず、`<=` を `<` にしても
通った（#703 のレビューの記録）。ちょうど閾値の提出が中段から最下段へ落ちる
退行を止める。

割合は `IndentReport` から直接作る ── 見たいのは割合と段階の対応で、字下げの
推定ではない。`1 / 5` は浮動小数でも `0.2` と同じ値になる。
"""

from __future__ import annotations

from aijudge_eval_c_indent_check import (
    DEFAULT_PARTIAL_MAX_RATIO,
    EVALUATOR_ID,
    IndentLine,
    IndentReport,
    judge_level,
)

from aijudge_core import RubricCriterion, RubricLevel
from aijudge_core.ids import CriterionId

COUNTED_LINES = 5
BROKEN_LINES = 1

CRITERION = RubricCriterion(
    id=CriterionId("crt_" + "1" * 32),
    code="indentation",
    title="インデント",
    description="入れ子の深さどおりに字下げされているか。",
    weight=1.0,
    levels=(
        RubricLevel(level=0, label="未達", descriptor="揃っていない", score_ratio=0.0),
        RubricLevel(level=1, label="一部", descriptor="一部崩れている", score_ratio=0.5),
        RubricLevel(level=2, label="達成", descriptor="揃っている", score_ratio=1.0),
    ),
    evaluator_id=EVALUATOR_ID,
)


def _report(counted: int, broken: int) -> IndentReport:
    lines = tuple(
        IndentLine(number=n, level=1, indent="    ", text="x;") for n in range(1, counted + 1)
    )
    return IndentReport(counted=lines, broken=lines[:broken], unit="    ")


def test_a_ratio_exactly_at_the_threshold_takes_the_middle_level() -> None:
    report = _report(COUNTED_LINES, BROKEN_LINES)
    assert report.ratio == DEFAULT_PARTIAL_MAX_RATIO
    assert judge_level(report, CRITERION, DEFAULT_PARTIAL_MAX_RATIO) == 1


def test_a_ratio_just_above_the_threshold_takes_the_bottom_level() -> None:
    """上の試験と対にする。閾値を超えたら最下段。"""
    report = _report(COUNTED_LINES, BROKEN_LINES + 1)
    assert report.ratio > DEFAULT_PARTIAL_MAX_RATIO
    assert judge_level(report, CRITERION, DEFAULT_PARTIAL_MAX_RATIO) == 0
