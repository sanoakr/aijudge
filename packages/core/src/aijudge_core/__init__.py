"""aiJudge core — 科目非依存のドメインモデルとイベント契約。

このパッケージは全サブシステムが依存してよい唯一の層であり、
逆にこのパッケージは他のどのパッケージにも依存しない（P1 / P9）。
依存方向は `.importlinter` の contract で CI が強制する。

ここに入れてよいもの:
  - 科目に依存しない語彙（Task / Submission / GradingRun / KC / Skill / Credential）
  - サブシステム間のイベント契約
  - 上記に閉じた純粋な規則（集約、状態遷移、妥当性検査）

ここに入れてはいけないもの:
  - 「コードか数式かレポートか」の知識 → evaluators/ へ
  - I/O（DB・HTTP・LLM・ファイル）    → 各 packages/ へ
"""

from __future__ import annotations

from .events import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    CredentialIssued,
    DomainEvent,
    GradingCompleted,
    SkillStateUpdated,
    SubmissionCreated,
    TaskPublished,
)
from .grading import (
    BlindMark,
    CriterionScore,
    EvaluatorKind,
    EvaluatorResult,
    EvaluatorStatus,
    GradingContext,
    GradingRun,
    HumanReview,
    KcOutcome,
    ReviewPolicy,
    Routing,
    aggregate,
    renormalize,
    resolve_conflicts,
)
from .ids import derived_id, new_id, prefix_of
from .knowledge import KnowledgeComponent, QMatrixEntry
from .skill import Credential, CredentialExport, MasteryModel, SkillEvidence, SkillState
from .spans import (
    ArtifactSpan,
    CharSpan,
    CriterionEvidence,
    Evidence,
    LineSpan,
    RegionSpan,
    WholeSpan,
)
from .submission import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Submission,
    SubmissionState,
    TranscriptionMeta,
    assert_transition,
    can_transition,
)
from .task import (
    Provenance,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
    TestCase,
)
from .tenancy import Course, Enrollment, Role, Tenant

__all__ = [
    "EVENT_TYPES",
    "SCHEMA_VERSION",
    "Artifact",
    "ArtifactKind",
    "ArtifactRole",
    "ArtifactSpan",
    "BlindMark",
    "CharSpan",
    "Course",
    "Credential",
    "CredentialExport",
    "CredentialIssued",
    "CriterionEvidence",
    "CriterionScore",
    "DomainEvent",
    "Enrollment",
    "EvaluatorKind",
    "EvaluatorResult",
    "EvaluatorStatus",
    "Evidence",
    "GradingCompleted",
    "GradingContext",
    "GradingRun",
    "HumanReview",
    "KcOutcome",
    "KnowledgeComponent",
    "LineSpan",
    "MasteryModel",
    "Provenance",
    "QMatrixEntry",
    "RegionSpan",
    "ReviewPolicy",
    "ReviewState",
    "Role",
    "Routing",
    "RubricCriterion",
    "RubricLevel",
    "SkillEvidence",
    "SkillState",
    "SkillStateUpdated",
    "Submission",
    "SubmissionCreated",
    "SubmissionState",
    "Task",
    "TaskPublished",
    "TaskVersion",
    "Tenant",
    "TestCase",
    "TranscriptionMeta",
    "WholeSpan",
    "aggregate",
    "assert_transition",
    "can_transition",
    "derived_id",
    "new_id",
    "prefix_of",
    "renormalize",
    "resolve_conflicts",
]
