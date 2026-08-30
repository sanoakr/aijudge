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
from .finalization import (
    AUTOMATIC_JUSTIFICATION,
    Finalization,
    FinalizationSource,
    GradeWindow,
    auto_finalizable,
    blocks_finalization,
    bulk_finalizable,
    grace_minutes,
    grade_window,
    settles_at,
)
from .grading import (
    MIN_JUSTIFICATION_LENGTH,
    BlindMark,
    CriterionScore,
    EvaluatorKind,
    EvaluatorResult,
    EvaluatorStatus,
    FinalScore,
    GradingContext,
    GradingPhase,
    GradingRun,
    HumanReview,
    KcOutcome,
    LatePenalty,
    LatePenaltyStep,
    ReviewPolicy,
    ReviewRequest,
    Routing,
    aggregate,
    final_score,
    late_penalty_for,
    penalty_crosses_boundary,
    renormalize,
    resolve_conflicts,
)
from .ids import derived_id, new_id, prefix_of
from .knowledge import KnowledgeComponent, QMatrixEntry, kc_id_for, parse_kc_key
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
from .uploads import (
    ALL_UPLOAD_SUFFIXES,
    DEFAULT_UPLOAD_SUFFIXES,
    SUFFIX_GROUPS,
    SUFFIX_KINDS,
    allowed_suffixes,
    kind_for,
    normalize_suffixes,
)

__all__ = [
    "ALL_UPLOAD_SUFFIXES",
    "AUTOMATIC_JUSTIFICATION",
    "DEFAULT_UPLOAD_SUFFIXES",
    "EVENT_TYPES",
    "MIN_JUSTIFICATION_LENGTH",
    "SCHEMA_VERSION",
    "SUFFIX_GROUPS",
    "SUFFIX_KINDS",
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
    "FinalScore",
    "Finalization",
    "FinalizationSource",
    "GradeWindow",
    "GradingCompleted",
    "GradingContext",
    "GradingPhase",
    "GradingRun",
    "HumanReview",
    "KcOutcome",
    "KnowledgeComponent",
    "LatePenalty",
    "LatePenaltyStep",
    "LineSpan",
    "MasteryModel",
    "Provenance",
    "QMatrixEntry",
    "RegionSpan",
    "ReviewPolicy",
    "ReviewRequest",
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
    "allowed_suffixes",
    "assert_transition",
    "auto_finalizable",
    "blocks_finalization",
    "bulk_finalizable",
    "can_transition",
    "derived_id",
    "final_score",
    "grace_minutes",
    "grade_window",
    "kc_id_for",
    "kind_for",
    "late_penalty_for",
    "new_id",
    "normalize_suffixes",
    "parse_kc_key",
    "penalty_crosses_boundary",
    "prefix_of",
    "renormalize",
    "resolve_conflicts",
    "settles_at",
]
