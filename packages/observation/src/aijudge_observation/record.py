"""観測レコードの型。

1 提出 × 1 観点ぶんの観測。測定はこれだけを読む（ADR 0007）。
観測は投影（projection）であって記録の正本ではない。正本は `GradingRun`
（不変・P8）と教員の確定採点で、そちらは上書きしない。観測は後から新しい
情報が付いた時点で書き直してよい。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Observation(BaseModel):
    """1 提出 × 1 観点の観測。

    段階の集合（`levels`）を各レコードに焼き込むのが要点。ここを課題定義から
    引き直す設計にすると、測定に採点の語彙が必要になり、引けなかったときに
    既定値へ黙って落ちる（QWK の重み行列が狂って誤った値が出る）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- 何を見た観測か --------------------------------------------------
    subject_profile: str = Field(min_length=1)
    task_name: str = Field(min_length=1)
    submission: str = Field(min_length=1)
    criterion_code: str = Field(min_length=1)
    # この観点が採りうる段階（昇順）。QWK の重み行列はこれで決まる。
    levels: tuple[int, ...] = Field(min_length=2)

    # -- 機械の判定 ------------------------------------------------------
    machine_level: int | None = None
    machine_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # 決定的評価が確定させた観点。AI の精度測定からは外す（AI は関与していない）。
    conclusive: bool = False
    # 採点できなかった観点。一致とも不一致とも数えない（ADR 0005）。
    unscored: bool = False
    grading_run_id: str | None = None

    # -- 人間の判定 ------------------------------------------------------
    # AI の判定を見る前に付けた段階。これだけが正解データになる。
    human_level: int | None = None
    # AI を見ずに付けたか。偽なら一致度の標本にしない。
    blind: bool = False
    marker: str | None = None

    # -- 提出単位の状態（同じ提出の全観点で同じ値）----------------------
    auto_confirmed: bool = False
    # 教員が AI を見たあとに段階を変えたか。未確定なら None。
    changed_after_seeing_ai: bool | None = None

    observed_at: datetime

    @property
    def submission_key(self) -> str:
        return f"{self.task_name}/{self.submission}"

    @property
    def usable_for_agreement(self) -> bool:
        """一致度の標本に数えてよいか。

        除外するもの:
          - 機械か人間のどちらかが判定していない
          - blind でない教員採点（AI に引きずられている）
          - 決定的評価が確定させた観点（AI の精度ではない）
          - 採点できなかった観点
        """
        return (
            self.machine_level is not None
            and self.human_level is not None
            and self.blind
            and not self.conclusive
            and not self.unscored
        )
