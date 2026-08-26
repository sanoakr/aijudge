"""aiJudge analytics (S9) — 採点一致度の測定と合格基準の判定。

PoC の合格判定はここが唯一の根拠。純関数だけで構成し、
採点の実装にも I/O にも依存しない。
"""

from __future__ import annotations

from .gates import Check, CriterionGate, Gates, Verdict, evaluate_gates, overall
from .metrics import (
    AgreementReport,
    agreement_report,
    cohen_kappa,
    confusion_matrix,
    exact_agreement,
    miss_rate,
    population_stdev,
    quadratic_weighted_kappa,
    review_rate,
)

__all__ = [
    "AgreementReport",
    "Check",
    "CriterionGate",
    "Gates",
    "Verdict",
    "agreement_report",
    "cohen_kappa",
    "confusion_matrix",
    "evaluate_gates",
    "exact_agreement",
    "miss_rate",
    "overall",
    "population_stdev",
    "quadratic_weighted_kappa",
    "review_rate",
]
