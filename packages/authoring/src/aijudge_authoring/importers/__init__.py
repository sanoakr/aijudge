"""既存資産の取り込み（S2）。

`sharif_judge` — Sharif Judge の課題ディレクトリを `TaskVersion` にする。
`companion`    — クライアント／サーバ課題の伴走プロセス宣言（ADR 0008）。
"""

from __future__ import annotations

from . import companion, sharif_judge

__all__ = ["companion", "sharif_judge"]
