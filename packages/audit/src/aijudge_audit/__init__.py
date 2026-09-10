"""監査ログ（3 つあるログのうちの 2 つめ）。

**誰が**成績に届く何を変えたか。DB に append-only で残し、書けなければ
操作ごと失敗させる。運用ログ（`aijudge_telemetry`・消えてよい）とも、
採点記録（`GradingRun`・採点の実体）とも別物である ── ADR 0016。

このパッケージが独立しているのは `packages/observation` と同じ理由による。
記録の型を使う側のパッケージに置くと、そのパッケージを消したときに他が
壊れる（ADR 0007 の実例）。
"""

from __future__ import annotations

from .event import MAX_DETAIL_CHARS, ActorKind, AuditAction, AuditEvent
from .memory import InMemoryAuditLog
from .protocols import AuditLog, DuplicateAuditEvent
from .recorder import AuditRecorder

__all__ = [
    "MAX_DETAIL_CHARS",
    "ActorKind",
    "AuditAction",
    "AuditEvent",
    "AuditLog",
    "AuditRecorder",
    "DuplicateAuditEvent",
    "InMemoryAuditLog",
]
