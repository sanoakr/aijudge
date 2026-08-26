"""aiJudge eval runner — 採点精度の測定コマンド。

合成の中心（composition root）なので複数のサブシステムに依存してよい。
サブシステムどうしは互いを import しない（ADR 0001）。
"""

from __future__ import annotations

from .golden import ENV_GOLDEN_DIR, GoldenItem, GoldenMark, GoldenSetError, load_golden
from .runner import EvalReport, ItemResult, run_evaluation

__all__ = [
    "ENV_GOLDEN_DIR",
    "EvalReport",
    "GoldenItem",
    "GoldenMark",
    "GoldenSetError",
    "ItemResult",
    "load_golden",
    "run_evaluation",
]
