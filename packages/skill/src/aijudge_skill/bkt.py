"""Bayesian Knowledge Tracing。

学習者がある KC を習得しているかを、観測（正誤の並び）から推定する。
持つのは 4 つのパラメータだけで、**推定は 1 本の漸化式**である。

    P(L0)  事前習得率      その KC を最初から知っている確率
    P(T)   学習率          1 回の機会で未習得から習得へ移る確率
    P(G)   当て推量        未習得なのに正答する確率
    P(S)   うっかり        習得しているのに誤答する確率

BKT を最初に置くのは、**説明できるから**である。習熟度 0.72 の根拠を
「この 5 件でこう動いた」と教員にも学習者にも示せる。DKT は精度で上回りうるが、
根拠を示せない推定を成績の隣に置くと、設計原則 P4（すべての判定は根拠を持つ）
が採点だけの規則になってしまう。

差し替えは `MasteryModel` で表現する。コアは「誰の・どの KC が・どれだけ」
しか知らず、推定の実装はこのパッケージに閉じる。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# 部分点を正誤に落とす閾値。
#
# **ルーブリック採点の観測は二値ではない。** BKT は正誤を前提にしているので
# どこかで畳む必要があり、ここに置いた。0.6 は合否境界（科目プロファイルの
# `review_policy.boundary_score` の既定）に合わせてある ── 「及第なら
# できたと見なす」という、教員に説明できる意味を持たせるため。
DEFAULT_CORRECT_THRESHOLD = 0.6


class BktParameters(BaseModel):
    """1 つの KC に対するパラメータ。

    既定値は文献で広く使われる出発点で、**実データで当てたものではない**。
    当てるには KC ごとに数十件の観測が要る（Phase 4 の合格基準が
    「次課題の正誤を AUC ≥ 0.70 で予測」なのはそのため）。それまでは
    この既定で走らせ、予測の当たり具合を観測として貯める。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # P(L0)
    prior: float = Field(default=0.2, ge=0.0, le=1.0)
    # P(T)
    learn: float = Field(default=0.15, ge=0.0, le=1.0)
    # P(G)。**上限を 0.5 で切る。** これを超えると「当てずっぽうの方が
    # 当たる」ことになり、正答が習得の証拠として働かなくなる（推定が反転する）。
    guess: float = Field(default=0.20, ge=0.0, le=0.5)
    # P(S)。同じ理由で 0.5 未満。
    slip: float = Field(default=0.10, ge=0.0, lt=0.5)


def posterior(prior: float, correct: bool, params: BktParameters) -> float:
    """観測 1 件を見たあとの習得確率（ベイズ更新 → 学習の遷移）。

    2 段であることが要点。まず「いま習得しているか」を観測で更新し、
    そのあとで「この機会に習得したかもしれない」を足す。順序を入れ替えると、
    誤答した直後に習熟度が上がることがある。
    """
    if correct:
        numerator = prior * (1.0 - params.slip)
        denominator = numerator + (1.0 - prior) * params.guess
    else:
        numerator = prior * params.slip
        denominator = numerator + (1.0 - prior) * (1.0 - params.guess)

    # 分母が 0 になるのは slip=0 かつ guess=0 のような端の設定のときだけ。
    # そのときは観測から何も学べないので、事前分布をそのまま通す。
    updated = prior if denominator <= 0.0 else numerator / denominator
    return updated + (1.0 - updated) * params.learn


def predict_correct(mastery: float, params: BktParameters) -> float:
    """次の 1 問に正答する確率。

    **習熟度そのものではない。** 習得していても slip で落とし、未習得でも
    guess で当たる。Phase 4 の合格基準（次課題の正誤を AUC ≥ 0.70 で予測）が
    見るのはこちらの値であって、`SkillState.mastery` ではない。
    """
    return mastery * (1.0 - params.slip) + (1.0 - mastery) * params.guess


def is_correct(score_ratio: float, threshold: float = DEFAULT_CORRECT_THRESHOLD) -> bool:
    """部分点を正誤に畳む。"""
    return score_ratio >= threshold
