"""aiJudge grading (S5) — 科目非依存の採点エンジン。

このパッケージは個々の Evaluator を import しない。
Evaluator は entry point で発見し、科目プロファイル（subjects/*.yaml）が
名前で指名する。これにより新科目の追加でエンジンが変わらない（ADR 0002）。
"""

from __future__ import annotations

from .observations import project_observations
from .overrides import ALLOWED_KEYS, LOCKED_KEYS, OverrideError
from .overrides import effective as effective_profile
from .pipeline import (
    PIPELINE_VERSION,
    ContentLoader,
    GradingPipeline,
    NoDeterministicWork,
    compute_input_hash,
    derive_kc_outcomes,
    grading_completed_event,
)
from .profile import (
    InputPolicy,
    MeasurementPolicy,
    SubjectProfile,
    load_profile,
    load_profiles,
)
from .protocol import EvaluationOutcome, EvaluationRequest, Evaluator, Normalizer
from .registry import (
    ENTRY_POINT_GROUP,
    NORMALIZER_ENTRY_POINT_GROUP,
    EvaluatorRegistry,
    NormalizerRegistry,
    default_normalizers,
    default_registry,
)

__all__ = [
    "ALLOWED_KEYS",
    "ENTRY_POINT_GROUP",
    "LOCKED_KEYS",
    "NORMALIZER_ENTRY_POINT_GROUP",
    "PIPELINE_VERSION",
    "ContentLoader",
    "EvaluationOutcome",
    "EvaluationRequest",
    "Evaluator",
    "EvaluatorRegistry",
    "GradingPipeline",
    "InputPolicy",
    "MeasurementPolicy",
    "NoDeterministicWork",
    "Normalizer",
    "NormalizerRegistry",
    "OverrideError",
    "SubjectProfile",
    "compute_input_hash",
    "default_normalizers",
    "default_registry",
    "derive_kc_outcomes",
    "effective_profile",
    "grading_completed_event",
    "load_profile",
    "load_profiles",
    "project_observations",
]
