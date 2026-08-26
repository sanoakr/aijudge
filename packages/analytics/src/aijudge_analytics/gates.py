"""合格基準の判定（S9）。

PoC-1 の合格基準を設定として持ち、測定結果と突き合わせる。

判定は 3 値であって 2 値ではない。

    PASS         基準を満たした
    FAIL         基準を満たさなかった
    NOT_MEASURED 判断できるだけのデータが無い

「測れていない」を「合格」に丸めないことがこのモジュールの存在理由。
標本が 3 件しかないのに κ = 0.8 が出たからといって合格ではない。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .metrics import AgreementReport


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_MEASURED = "not_measured"


class CriterionGate(BaseModel):
    """1 観点に課す基準。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_cohen_kappa: float | None = None
    min_quadratic_weighted_kappa: float | None = None
    min_exact_agreement: float | None = None


class Gates(BaseModel):
    """PoC の合格基準一式。`evals/gates.yaml` から読む。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poc: str = Field(min_length=1)
    # これを下回る標本数では、どの指標も判定しない。
    min_sample_size: int = Field(default=30, ge=1)
    criteria: dict[str, CriterionGate] = Field(default_factory=dict)
    max_miss_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    max_review_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # 同一提出を繰り返し採点したときの標準偏差の上限（満点に対する比）。
    max_score_stdev: float | None = Field(default=None, ge=0.0, le=1.0)


class Check(BaseModel):
    """基準 1 つの判定結果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    verdict: Verdict
    observed: float | None = None
    threshold: float | None = None
    detail: str = ""

    @property
    def symbol(self) -> str:
        return {Verdict.PASS: "PASS", Verdict.FAIL: "FAIL", Verdict.NOT_MEASURED: "  — "}[
            self.verdict
        ]


def _at_least(
    name: str, observed: float | None, threshold: float | None, *, reason: str
) -> Check | None:
    if threshold is None:
        return None
    if observed is None:
        return Check(name=name, verdict=Verdict.NOT_MEASURED, threshold=threshold, detail=reason)
    return Check(
        name=name,
        verdict=Verdict.PASS if observed >= threshold else Verdict.FAIL,
        observed=observed,
        threshold=threshold,
    )


def _at_most(
    name: str, observed: float | None, threshold: float | None, *, reason: str
) -> Check | None:
    if threshold is None:
        return None
    if observed is None:
        return Check(name=name, verdict=Verdict.NOT_MEASURED, threshold=threshold, detail=reason)
    return Check(
        name=name,
        verdict=Verdict.PASS if observed <= threshold else Verdict.FAIL,
        observed=observed,
        threshold=threshold,
    )


def evaluate_gates(
    gates: Gates,
    reports: dict[str, AgreementReport],
    *,
    observed_miss_rate: float | None = None,
    observed_review_rate: float | None = None,
    observed_score_stdev: float | None = None,
) -> tuple[Check, ...]:
    """基準と測定結果を突き合わせる。"""
    checks: list[Check] = []

    for code, gate in sorted(gates.criteria.items()):
        report = reports.get(code)
        if report is None or report.sample_size < gates.min_sample_size:
            have = 0 if report is None else report.sample_size
            reason = f"{have}/{gates.min_sample_size} 件しか教員採点がない"
            for name, threshold in (
                (f"{code}: Cohen's κ", gate.min_cohen_kappa),
                (f"{code}: QWK", gate.min_quadratic_weighted_kappa),
                (f"{code}: 完全一致率", gate.min_exact_agreement),
            ):
                if threshold is not None:
                    checks.append(
                        Check(
                            name=name,
                            verdict=Verdict.NOT_MEASURED,
                            threshold=threshold,
                            detail=reason,
                        )
                    )
            continue

        insufficient = "標本不足"
        for check in (
            _at_least(
                f"{code}: Cohen's κ",
                report.cohen_kappa,
                gate.min_cohen_kappa,
                reason=insufficient,
            ),
            _at_least(
                f"{code}: QWK",
                report.quadratic_weighted_kappa,
                gate.min_quadratic_weighted_kappa,
                reason=insufficient,
            ),
            _at_least(
                f"{code}: 完全一致率",
                report.exact_agreement,
                gate.min_exact_agreement,
                reason=insufficient,
            ),
        ):
            if check is not None:
                checks.append(check)

    for check in (
        _at_most(
            "見逃し率",
            observed_miss_rate,
            gates.max_miss_rate,
            reason="自動確定した提出に対する教員の確認結果がない",
        ),
        _at_most(
            "レビュー行き率",
            observed_review_rate,
            gates.max_review_rate,
            reason="採点結果がない",
        ),
        _at_most(
            "採点のばらつき",
            observed_score_stdev,
            gates.max_score_stdev,
            reason="同一提出の反復採点をしていない",
        ),
    ):
        if check is not None:
            checks.append(check)

    return tuple(checks)


def overall(checks: tuple[Check, ...]) -> Verdict:
    """全体の判定。

    1 つでも FAIL があれば FAIL。FAIL は無いが NOT_MEASURED があれば
    NOT_MEASURED。**測れていないものを合格にはしない。**
    """
    if not checks:
        return Verdict.NOT_MEASURED
    if any(check.verdict is Verdict.FAIL for check in checks):
        return Verdict.FAIL
    if any(check.verdict is Verdict.NOT_MEASURED for check in checks):
        return Verdict.NOT_MEASURED
    return Verdict.PASS
