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
from .protocol import (
    TEST_CASE_SHAPES,
    EvaluationOutcome,
    EvaluationRequest,
    Evaluator,
    reads_test_cases,
    test_case_shape,
)
from .registry import (
    ENTRY_POINT_GROUP,
    EXTRACTOR_ENTRY_POINT_GROUP,
    EvaluatorRegistry,
    ExtractorRegistry,
    default_extractors,
    default_registry,
)

__all__ = [
    "ALLOWED_KEYS",
    "ENTRY_POINT_GROUP",
    "EXTRACTOR_ENTRY_POINT_GROUP",
    "LOCKED_KEYS",
    "PIPELINE_VERSION",
    "TEST_CASE_SHAPES",
    "ContentLoader",
    "EvaluationOutcome",
    "EvaluationRequest",
    "Evaluator",
    "EvaluatorRegistry",
    "ExtractorRegistry",
    "GradingPipeline",
    "InputPolicy",
    "MeasurementPolicy",
    "OverrideError",
    "SubjectProfile",
    "compute_input_hash",
    "default_extractors",
    "default_registry",
    "derive_kc_outcomes",
    "effective_profile",
    "grading_completed_event",
    "load_profile",
    "load_profiles",
    "project_observations",
    "reads_test_cases",
    "test_case_shape",
]
