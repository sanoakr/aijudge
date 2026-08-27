"""aiJudge grader — 採点ワーカー（合成の中心）。

S3 のキューと S5 のパイプラインを束ねる唯一の層。提出受付は採点エンジンを
知らず、採点エンジンはキューを知らない（ADR 0001）。

    aijudge-worker --once     # キューが空になるまで処理して終わる
    aijudge-worker            # 常駐して待つ
"""

from __future__ import annotations

from .relay import EventRelay
from .worker import (
    DEFAULT_LEASE_SECONDS,
    GradingWorker,
    PermanentGradingError,
    WorkResult,
    import_task_version,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "EventRelay",
    "GradingWorker",
    "PermanentGradingError",
    "WorkResult",
    "import_task_version",
]
