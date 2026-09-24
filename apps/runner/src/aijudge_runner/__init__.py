"""aiJudge runner — ブラウザ IDE の試しの実行を sandbox で動かす（ADR 0024）。

**コードを動かせる合成ルートは、採点ワーカーとここだけである。** web は
`run_requests` に行を書くだけで、Docker の権限を持たない（不変条件 I6）。
"""

from __future__ import annotations

from .runner import CodeRunner, RunNotPossible, limits_for

__all__ = ["CodeRunner", "RunNotPossible", "limits_for"]
