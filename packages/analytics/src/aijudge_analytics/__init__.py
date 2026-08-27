"""aiJudge analytics (S9) — 採点一致度の測定と合格基準の判定。

採点性能の判定はここが唯一の根拠。純関数だけで構成し、
採点の実装にも I/O にも依存しない。

**このパッケージは採点の必須経路ではない。** 削除しても採点は動く
（ADR 0007）。測定は記録済みの観測レコード（`Observation`）を読むだけで、
採点を実行しない。
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
from .observation import MeasurementSummary, summarize

__all__ = [
    "AgreementReport",
    "Check",
    "CriterionGate",
    "Gates",
    "MeasurementSummary",
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
    "summarize",
]
