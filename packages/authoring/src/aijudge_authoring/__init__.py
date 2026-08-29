"""aiJudge authoring (S2) — 課題・ルーブリック・テストケースの作成と取り込み。

AI 作問はこのパッケージのワーカーとして Phase 4 で足す。既存資産
（Sharif Judge の課題ディレクトリ）の取り込みを先にするのは、
「教員が課題を作り直さずに新システムを試せる」ことが Phase 0 を
実運用に載せる条件だから。

課題の保存先の契約もここに置く。課題は S2 の持ち物で、採点側と提出側は
読むだけ。両者が別々に取り込むと、表示している観点と採点した観点が
食い違いうる。
"""

from __future__ import annotations

from .importers import sharif_judge
from .repository import (
    InMemoryTaskRepository,
    TaskImmutabilityViolation,
    TaskRepository,
    TaskStoreError,
    substantive,
)
from .solvability import SolvabilityOutcome, SolvabilityReport, SolverAttempt
from .spec import (
    AI_EVALUATOR,
    DEFAULT_EVALUATOR,
    CriterionSpec,
    LevelSpec,
    TaskSpec,
    TestCaseSpec,
    build_task_version,
)
from .statement import render_statement
from .verification import (
    DEFAULT_MIN_KILL_RATIO,
    GateOutcome,
    Mutation,
    MutationKind,
    MutationOutcome,
    TaskChecks,
    VerificationReport,
    mutate,
)

__all__ = [
    "AI_EVALUATOR",
    "DEFAULT_EVALUATOR",
    "DEFAULT_MIN_KILL_RATIO",
    "CriterionSpec",
    "GateOutcome",
    "InMemoryTaskRepository",
    "LevelSpec",
    "Mutation",
    "MutationKind",
    "MutationOutcome",
    "SolvabilityOutcome",
    "SolvabilityReport",
    "SolverAttempt",
    "TaskChecks",
    "TaskImmutabilityViolation",
    "TaskRepository",
    "TaskSpec",
    "TaskStoreError",
    "TestCaseSpec",
    "VerificationReport",
    "build_task_version",
    "mutate",
    "render_statement",
    "sharif_judge",
    "substantive",
]
