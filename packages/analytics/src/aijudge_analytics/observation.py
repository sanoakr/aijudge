"""観測から指標を計算する（S9）。

**測定は採点を実行しない。** 採点とレビューが残した記録から、
1 提出 × 1 観点ぶんの観測を投影しておき、指標はそれだけを読んで計算する
（ADR 0007）。これにより κ の再計算に LLM もサンドボックスも課題定義も要らない。

観測レコードは投影（projection）であって記録の正本ではない。正本は
`GradingRun`（不変・P8）と教員の確定採点で、そちらは上書きしない。
観測は後から新しい情報が付いた時点で書き直してよい。

このモジュールは純関数と型だけを持つ。ファイルの読み書きは app 層の責務。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from aijudge_observation import Observation

from .metrics import AgreementReport, agreement_report, miss_rate, review_rate


class MeasurementSummary(BaseModel):
    """観測から計算した測定結果。ここまで純関数で出る。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_count: int = Field(ge=0)
    submission_count: int = Field(ge=0)
    blind_submission_count: int = Field(ge=0)
    agreement: dict[str, AgreementReport] = Field(default_factory=dict)
    observed_miss_rate: float | None = None
    observed_review_rate: float | None = None
    # 一致度の標本から外した観測の理由別内訳。黙って減らさないため。
    excluded: dict[str, int] = Field(default_factory=dict)


def _levels_for(code: str, group: Sequence[Observation]) -> tuple[int, ...]:
    """観点の段階。同一観点で食い違っていたら例外にする。

    課題によって段階数が違うまま QWK を計算すると、重み行列が実態と合わない。
    黙って片方に寄せず、構成の誤りとして落とす。
    """
    distinct = {observation.levels for observation in group}
    if len(distinct) > 1:
        raise ValueError(
            f"criterion {code!r} is observed with conflicting level sets: {sorted(distinct)}"
        )
    return next(iter(distinct))


def summarize(observations: Iterable[Observation]) -> MeasurementSummary:
    """観測から一致度と運用指標を計算する。

    **採点は行わない。** 記録済みの観測だけを読む。
    """
    items = list(observations)
    if not items:
        return MeasurementSummary(observation_count=0, submission_count=0, blind_submission_count=0)

    # -- 観点ごとの一致度 -------------------------------------------------
    usable: dict[str, list[Observation]] = {}
    excluded: dict[str, int] = {}
    for observation in items:
        if observation.usable_for_agreement:
            usable.setdefault(observation.criterion_code, []).append(observation)
            continue
        excluded[_exclusion_reason(observation)] = (
            excluded.get(_exclusion_reason(observation), 0) + 1
        )

    agreement: dict[str, AgreementReport] = {}
    for code, group in usable.items():
        agreement[code] = agreement_report(
            code,
            [observation.human_level for observation in group],  # type: ignore[misc]
            [observation.machine_level for observation in group],  # type: ignore[misc]
            _levels_for(code, group),
        )

    # -- 提出単位の指標 ---------------------------------------------------
    # 観点ごとのレコードから提出単位に畳む。auto_confirmed と
    # machine_corrected は提出単位の値なので、代表を 1 つ取る。
    by_submission: dict[str, Observation] = {}
    for observation in items:
        by_submission.setdefault(observation.submission_key, observation)

    graded = [
        observation
        for observation in by_submission.values()
        if observation.grading_run_id is not None
    ]
    observed_review_rate = (
        review_rate([observation.auto_confirmed for observation in graded]) if graded else None
    )

    # 見逃し率は「教員が機械の判定を直したか」で測る。未確定の提出は
    # 分母に入れない（直したかどうかを知る方法がない）。
    finalized = [observation for observation in graded if observation.machine_corrected is not None]
    observed_miss = (
        miss_rate(
            [observation.auto_confirmed for observation in finalized],
            [bool(observation.machine_corrected) for observation in finalized],
        )
        if finalized
        else None
    )

    return MeasurementSummary(
        observation_count=len(items),
        submission_count=len(by_submission),
        blind_submission_count=sum(
            1 for observation in by_submission.values() if observation.blind
        ),
        agreement=agreement,
        observed_miss_rate=observed_miss,
        observed_review_rate=observed_review_rate,
        excluded=dict(sorted(excluded.items())),
    )


def _exclusion_reason(observation: Observation) -> str:
    """一致度の標本から外した理由。レポートに出して隠さない。"""
    if observation.unscored:
        return "採点できなかった観点"
    if observation.conclusive:
        return "決定的評価が確定（AI は関与しない）"
    if observation.machine_level is None:
        return "採点がまだ届いていない"
    if observation.human_level is None:
        return "教員採点がない"
    if not observation.blind:
        return "blind でない教員採点"
    return "その他"
