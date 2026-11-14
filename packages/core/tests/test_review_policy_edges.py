"""`ReviewPolicy.requires_review` の境目を固定する（#684・P5 の振り分け本体）。

確信度の分岐そのものは `evals/test_ai_judge.py` が見ているが、「閾値ちょうど」
「重みの超過」「合否境界ちょうど」は core の単体で見ていなかった。`<` と `<=` を
入れ替えても、重みの分岐を消してもスイートは通っていた。

向きには意味がある。`subjects/demo_image.yaml` は `confidence_below: 1.0` で
「確信度 1.0 未満は全部人が見る」を表しており、`<` が `<=` に化けると確信度 1.0
の判定までレビューに回る。
"""

from __future__ import annotations

import pytest

from aijudge_core import CharSpan, CriterionScore, EvaluatorKind, Evidence, ReviewPolicy
from aijudge_core.ids import ArtifactId, CriterionId, CriterionScoreId, EvaluatorResultId


def _ai_score(*, confidence: float, weight: float = 1.0) -> CriterionScore:
    return CriterionScore(
        id=CriterionScoreId("cs_" + "1" * 32),
        criterion_id=CriterionId("crt_" + "1" * 32),
        evaluator_result_id=EvaluatorResultId("evr_" + "a" * 32),
        kind=EvaluatorKind.AI,
        level=1,
        score_ratio=0.5,
        weight=weight,
        confidence=confidence,
        evidence=(
            Evidence(
                artifact_id=ArtifactId("art_" + "5" * 32),
                artifact_content_hash="sha256:x",
                span=CharSpan(start=0, end=10),
            ),
        ),
        rationale="判定の理由。",
    )


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.74, True), (0.75, False), (0.76, False)],
    ids=["below", "at", "above"],
)
def test_confidence_below_the_threshold_is_strict(confidence: float, expected: bool) -> None:
    """閾値**未満**で人へ回す。ちょうどは回さない。"""
    policy = ReviewPolicy(confidence_below=0.75)
    assert policy.requires_review((_ai_score(confidence=confidence),), 1.0) is expected


def test_a_threshold_of_one_sends_everything_short_of_certainty() -> None:
    """`demo_image.yaml` の使い方。確信度 1.0 だけが人を通らない。"""
    policy = ReviewPolicy(confidence_below=1.0)
    assert policy.requires_review((_ai_score(confidence=0.99),), 1.0)
    assert not policy.requires_review((_ai_score(confidence=1.0),), 1.0)


def test_a_criterion_heavier_than_the_limit_goes_to_review_however_confident() -> None:
    """重い観点は、確信度が高くても人が見る。ちょうどの重みは回さない。"""
    policy = ReviewPolicy(confidence_below=0.0, always_review_if_weight_over=0.5)
    assert policy.requires_review((_ai_score(confidence=1.0, weight=0.6),), 1.0)
    assert not policy.requires_review((_ai_score(confidence=1.0, weight=0.5),), 1.0)


@pytest.mark.parametrize(
    ("total", "expected"),
    [(0.25, True), (0.75, True), (0.5, True), (0.2, False), (0.8, False)],
    ids=["lower-edge", "upper-edge", "on-boundary", "below-band", "above-band"],
)
def test_the_boundary_band_includes_its_edges(total: float, expected: bool) -> None:
    """合否境界 ± マージンの**両端を含む**。

    値は 2 進で正確に表せるもの（0.5 ± 0.25）を選び、浮動小数の誤差で端が
    どちらにも転ばないようにしている。
    """
    policy = ReviewPolicy(confidence_below=0.0, boundary_score=0.5, boundary_margin=0.25)
    assert policy.requires_review((_ai_score(confidence=1.0),), total) is expected
