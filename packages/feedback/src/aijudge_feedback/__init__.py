"""aiJudge feedback — 採点結果を「次の一手」に変える。

点数の説明ではない。「テストケース 3 で落ちました」は結果の反復であって
助けにならない。

**確定していない AI の判定を材料にしない。** それを材料にすると、学習者に
見せないことにした判断が文章の形で漏れる（設計原則 P5）。即座に返すのは
決定的評価の結果に基づく助言だけで、ルーブリック判定に基づくものは
教員の確定後に根拠と一緒に見える。

S6 が停止していても無言にはしない。LLM を使わない要約に落ちる（P2）。
"""

from __future__ import annotations

from .generator import (
    FEEDBACK_PROMPT,
    MAX_FEEDBACK_CHARS,
    MAX_SOURCE_CHARS,
    FeedbackGenerator,
    FeedbackResult,
    NextStep,
    releasable_scores,
    summarize_findings,
)

__all__ = [
    "FEEDBACK_PROMPT",
    "MAX_FEEDBACK_CHARS",
    "MAX_SOURCE_CHARS",
    "FeedbackGenerator",
    "FeedbackResult",
    "NextStep",
    "releasable_scores",
    "summarize_findings",
]
