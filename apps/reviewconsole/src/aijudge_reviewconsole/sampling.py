"""blind 採点の抽出。

**全件 blind にしない。** レビュー 1 件ごとに 2 段階の入力を強制することに
なり、測定コストを日常業務に転嫁する（ADR 0007）。

**教員に選ばせない。** 難しい提出だけ blind にするといった選択バイアスが
入ると、一致度がその分だけ意味を失う。

したがって提出 ID のハッシュで決める。決定的（同じ提出は毎回同じ判定）で、
比率は科目プロファイルが宣言する。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from aijudge_core import RubricCriterion

_SAMPLE_BITS = 64


def is_blind_sample(submission_id: str, rate: float) -> bool:
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(submission_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / (1 << _SAMPLE_BITS)
    return bucket < rate


def judged_by_ai(criteria: Iterable[RubricCriterion], ai_evaluators: Iterable[str]) -> bool:
    """この観点の組に、AI が判定するものが 1 つでもあるか。

    blind 採点は AI と人の一致度を測るためのもの（ADR 0005）。AI が何も判定
    しない課題（決定的評価器だけ・人が採点する観点だけ）には測る相手がおらず、
    blind を求めても人の手間になるだけである。`Pipeline.has_ai_work` と同じ
    見方 ── 人が採点する観点は AI に渡らず、評価器を名指した観点はその評価器
    が AI 評価器のときだけ AI が判定する（指定が無ければプロファイルの AI 評価器）。
    """
    ai = set(ai_evaluators)
    if not ai:
        return False
    return any(
        not criterion.scored_by_human
        and (criterion.evaluator_id is None or criterion.evaluator_id in ai)
        for criterion in criteria
    )
