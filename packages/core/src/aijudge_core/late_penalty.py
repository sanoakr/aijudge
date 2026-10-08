"""遅延の減点の段と、どの段が効くかの決め方。

置き場所を `grading.py` から分けた理由: `Task` が自分の段を持つ（ユニット単位の
上書き）ので `task.py` から読める位置に要るが、`grading.py` は `task.py` を読む。
同じ場所に置くと循環する。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["LatePenaltyStep", "check_steps_sorted"]


class LatePenaltyStep(BaseModel):
    """遅延の段。「この時間を超えたらこの割合を引く」。

    `after_hours` は締切からの超過時間で、`ratio` は総合点比から差し引く量。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    after_hours: float = Field(ge=0.0)
    ratio: float = Field(ge=0.0, le=1.0)


def check_steps_sorted(steps: tuple[LatePenaltyStep, ...]) -> None:
    """段は超過時間の昇順で、同じ時間を重ねない（`Course` と `Task` で同じ規則）。"""
    hours = [step.after_hours for step in steps]
    if hours != sorted(set(hours)):
        raise ValueError("late_penalty_steps must be sorted by after_hours and unique")
