"""記録された観測レコードから採点精度を測る。

**このモジュールは採点を実行しない。** 観測レコード（`Observation`）を読み、
指標を計算し、合格基準と突き合わせるだけ（ADR 0007）。以前は測定のために
ゴールデンセットを採点し直していたが、それは:

- 測定に LLM とサンドボックスと課題定義を要求し、
- 教員が実際に見たのとは別の採点について見逃し率を出し、
- 「測定を必須機能にしない」という方針と両立しなかった。

観測レコードは採点とレビューが残した記録からの投影で、採点側が書き出す。
測定側は読むだけなので、採点の語彙（`GradingRun` や `TaskVersion`）を知らない。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aijudge_analytics import (
    AgreementReport,
    Check,
    Gates,
    MeasurementSummary,
    Observation,
    Verdict,
    evaluate_gates,
    overall,
    summarize,
)


class EvalReport(BaseModel):
    """測定結果一式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poc: str
    subject_profile: str
    generated_at: datetime
    observation_count: int = 0
    submission_count: int = 0
    blind_submission_count: int = 0
    agreement: dict[str, AgreementReport] = Field(default_factory=dict)
    observed_miss_rate: float | None = None
    observed_review_rate: float | None = None
    # 一致度の標本から外した観測の理由別内訳。黙って減らさない（ADR 0005）。
    excluded: dict[str, int] = Field(default_factory=dict)
    checks: tuple[Check, ...] = ()
    verdict: Verdict = Verdict.NOT_MEASURED


def measure(
    observations: Iterable[Observation],
    *,
    gates: Gates,
    subject_profile: str,
) -> EvalReport:
    """観測から測定結果を作る。副作用は無く、採点も呼ばない。"""
    summary: MeasurementSummary = summarize(observations)

    checks = evaluate_gates(
        gates,
        summary.agreement,
        observed_miss_rate=summary.observed_miss_rate,
        observed_review_rate=summary.observed_review_rate,
        # 反復採点によるばらつきは「実験」であって記録から測れない。
        # 測定リーダーの仕事ではないので、ここでは常に未測定とする。
        observed_score_stdev=None,
    )

    return EvalReport(
        poc=gates.poc,
        subject_profile=subject_profile,
        generated_at=datetime.now(UTC),
        observation_count=summary.observation_count,
        submission_count=summary.submission_count,
        blind_submission_count=summary.blind_submission_count,
        agreement=summary.agreement,
        observed_miss_rate=summary.observed_miss_rate,
        observed_review_rate=summary.observed_review_rate,
        excluded=summary.excluded,
        checks=checks,
        verdict=overall(checks),
    )
