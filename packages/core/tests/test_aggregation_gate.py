"""AND ゲートの規則を固定する（Issue #5）。

固定したいのは 3 つ。

宣言するまで変わらない  OR（既定）では何も打ち切らない。
上から見て最初の 0%     そこで止め、**以降だけ**を打ち切る。
判定できないところで止める
                        まだ点の付いていない観点に行き当たったら何も打ち切らない
                        ── その観点が 0% になるかはあとの段階で決まるので、
                        先回りして切ると、通っていた学習者の観点が 0% で確定する。
"""

from __future__ import annotations

from aijudge_core import (
    Aggregation,
    CriterionScore,
    EvaluatorKind,
    RubricCriterion,
    RubricLevel,
    effective_aggregation,
    gate_skipped,
)
from aijudge_core.ids import CriterionId, CriterionScoreId, EvaluatorResultId

C1 = CriterionId("crt_" + "1" * 32)
C2 = CriterionId("crt_" + "2" * 32)
C3 = CriterionId("crt_" + "3" * 32)


def _criterion(criterion_id: CriterionId, code: str) -> RubricCriterion:
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=code,
        description=f"{code} を見る観点。",
        weight=1 / 3,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="足りない", score_ratio=0.0),
            RubricLevel(level=1, label="達成", descriptor="十分", score_ratio=1.0),
        ),
    )


CRITERIA = (_criterion(C1, "runs"), _criterion(C2, "correct"), _criterion(C3, "readable"))


def _score(criterion_id: CriterionId, ratio: float) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId("cs_" + criterion_id[-1] * 32),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId("evr_" + "d" * 32),
        kind=EvaluatorKind.DETERMINISTIC,
        level=1 if ratio else 0,
        score_ratio=ratio,
        weight=1 / 3,
        confidence=1.0,
        conclusive=True,
        rationale="テストを実行した。",
    )


def test_or_never_cuts_anything() -> None:
    """**宣言するまで挙動は変わらない。** 既定は OR。"""
    scores = (_score(C1, 0.0), _score(C2, 1.0), _score(C3, 1.0))
    assert gate_skipped(CRITERIA, scores, Aggregation.OR) == ()


def test_and_cuts_everything_after_the_first_zero() -> None:
    scores = (_score(C1, 1.0), _score(C2, 0.0))
    assert gate_skipped(CRITERIA, scores, Aggregation.AND) == (C3,)


def test_and_cuts_nothing_when_the_top_criterion_passes() -> None:
    scores = (_score(C1, 1.0), _score(C2, 1.0), _score(C3, 0.0))
    # いちばん下が 0% でも、その後ろには何も無い。
    assert gate_skipped(CRITERIA, scores, Aggregation.AND) == ()


def test_and_waits_where_it_cannot_yet_decide() -> None:
    """**まだ判定の無い観点で走査を止める。**

    そこが 0% になるかどうかは AI 段階で決まる。先回りして後続を切ると、
    実際には通っていた学習者の観点が 0% で確定してしまう。
    """
    scores = (_score(C2, 0.0),)  # C1（先頭）はまだ判定が無い
    assert gate_skipped(CRITERIA, scores, Aggregation.AND) == ()


def test_the_task_setting_overrides_the_course_default() -> None:
    assert effective_aggregation(None, None) is Aggregation.OR
    assert effective_aggregation(None, Aggregation.AND) is Aggregation.AND
    assert effective_aggregation(Aggregation.OR, Aggregation.AND) is Aggregation.OR
    assert effective_aggregation(Aggregation.AND, Aggregation.OR) is Aggregation.AND
