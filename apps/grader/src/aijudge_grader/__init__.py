"""aiJudge grader — 採点ワーカー（合成の中心）。

S3 のキューと S5 のパイプラインを束ねる唯一の層。提出受付は採点エンジンを
知らず、採点エンジンはキューを知らない（ADR 0001）。

    aijudge-worker --once     # キューが空になるまで処理して終わる
    aijudge-worker            # 常駐して待つ
"""

from __future__ import annotations

from .feedback import build_feedback_generator
from .relay import EventRelay
from .skill_subscriber import SkillSubscriber, subscribe_skills
from .task_verifier import DEFAULT_MUTATION_LIMIT, TaskVerifier
from .worker import (
    DEFAULT_LEASE_SECONDS,
    GradingWorker,
    PermanentGradingError,
    WorkResult,
    import_task_version,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MUTATION_LIMIT",
    "EventRelay",
    "GradingWorker",
    "PermanentGradingError",
    "SkillSubscriber",
    "TaskVerifier",
    "WorkResult",
    "build_feedback_generator",
    "import_task_version",
    "subscribe_skills",
]
