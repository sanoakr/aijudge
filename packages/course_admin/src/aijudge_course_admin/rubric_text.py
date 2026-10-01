"""選んだ段階のルーブリックの記述から、確定の根拠の文を組む（2026-10-01）。

確定の根拠は必須で学習者に表示される（ADR 0009 §4）。空欄から書かせると、教員は
毎回「テストを通っており妥当」のような同じ文を打つことになり、人が採点する課題
（AI の素案が作れない）では特に手間だった。

**材料は教員が選んだ段階の記述だけ。** 段階の記述はその段階が何を意味するかを
教員自身が決めた文で、選んだこと自体が判断である。そこに無いことは足さない
（`justification.py` がモデルに作文させないのと同じ線引き）。教員は必要なら
書き足して確定する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aijudge_core import RubricCriterion
from aijudge_core.ids import CriterionId

# 一致して確定するときの書き出し。**誰が何をしたかを先に言う** ── 学習者には
# 機械の判定を追認しただけに見えないよう、教員が先に採点したことを書く。
AGREED_PREFIX = "教員が先に採点し、システムの判定と一致しました。"

# % を表すときの倍率。
PERCENT = 100


def level_lines(
    criteria: Sequence[RubricCriterion], levels: Mapping[CriterionId, int]
) -> list[str]:
    """観点ごとに「観点名: 段階名（得点）── 記述」の 1 行。段階の無い観点は飛ばす。"""
    lines: list[str] = []
    for criterion in criteria:
        chosen = levels.get(criterion.id)
        if chosen is None:
            continue
        level = next((lv for lv in criterion.levels if lv.level == chosen), None)
        if level is None:
            continue
        head = f"{criterion.title}: {level.label}（{level.score_ratio * PERCENT:.0f}%）"
        descriptor = level.descriptor.strip()
        lines.append(f"{head} ── {descriptor}" if descriptor else head)
    return lines


def rubric_justification(
    criteria: Sequence[RubricCriterion],
    levels: Mapping[CriterionId, int],
    *,
    agreed: bool = False,
) -> str:
    """確定の根拠の文。`agreed` なら一致して確定したことを書き出しに置く。"""
    lines = level_lines(criteria, levels)
    if agreed:
        lines.insert(0, AGREED_PREFIX)
    return "\n".join(lines)


__all__ = ["AGREED_PREFIX", "level_lines", "rubric_justification"]
