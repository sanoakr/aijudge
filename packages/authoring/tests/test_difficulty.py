"""難度推定の規則を固定する（S2、設計方針 §5）。

固定したいのは 4 つ。

少ない標本を率にしない 3 件中 2 件は 67% の証拠ではない（ADR 0005）。
1 件に賭けない       その課題の癖がそのまま予測になる。
当てられなければ出さない 数字を出さないことと、当てにならない数字を出すことは違う。
正答率 ≠ 難しさ      言葉にして必ず添える。
"""

from __future__ import annotations

import pytest

from aijudge_authoring.difficulty import (
    MIN_ATTEMPTS,
    DifficultyEstimate,
    TaskOutcomeStats,
    estimate,
)
from aijudge_authoring.similarity import SimilarityMethod, SimilarTask


def _near(vid: str, score: float) -> SimilarTask:
    return SimilarTask(
        task_version_id=vid, title=f"課題{vid}", score=score, method=SimilarityMethod.EMBEDDING
    )


def _stats(vid: str, attempts: int, passed: int) -> TaskOutcomeStats:
    return TaskOutcomeStats(task_version_id=vid, attempts=attempts, passed=passed)


def test_a_small_sample_is_not_a_pass_rate() -> None:
    """3 件中 2 件は 67% の証拠ではない。"""
    assert _stats("a", 3, 2).pass_rate is None
    assert _stats("a", MIN_ATTEMPTS, 10).pass_rate == pytest.approx(10 / MIN_ATTEMPTS)


def test_one_neighbour_is_not_enough() -> None:
    """**1 件に賭けない。** その課題の癖がそのまま予測になる。"""
    result = estimate(
        (_near("a", 0.9),),
        {"a": _stats("a", 40, 20)},
        method=SimilarityMethod.EMBEDDING,
    )
    assert result.verdict == "NOT_MEASURED"
    assert result.predicted_pass_rate is None


def test_neighbours_without_history_are_dropped_not_guessed() -> None:
    """実績の無い近傍は数に入れない。埋めると、無いデータで予測することになる。"""
    result = estimate(
        (_near("a", 0.9), _near("b", 0.8)),
        {"a": _stats("a", 40, 20), "b": _stats("b", 2, 1)},
        method=SimilarityMethod.EMBEDDING,
    )
    assert result.verdict == "NOT_MEASURED"
    assert "1 件しかありません" in result.reason


def test_the_estimate_is_weighted_by_similarity() -> None:
    """近い課題ほど強く効く。"""
    result = estimate(
        (_near("a", 1.0), _near("b", 0.0)),
        {"a": _stats("a", 40, 40), "b": _stats("b", 40, 0)},
        method=SimilarityMethod.EMBEDDING,
    )
    assert result.predicted_pass_rate == pytest.approx(1.0)


def test_the_summary_says_a_pass_rate_is_not_difficulty() -> None:
    result = estimate(
        (_near("a", 0.9), _near("b", 0.7)),
        {"a": _stats("a", 40, 12), "b": _stats("b", 30, 21)},
        method=SimilarityMethod.EMBEDDING,
    )
    text = result.summary()
    assert "正答率は難しさそのものではありません" in text
    # 根拠にした課題を名指しする。数字だけでは確かめようがない。
    assert "課題a" in text


def test_lexical_neighbours_are_flagged_as_weaker_evidence() -> None:
    """字面で選んだ近傍は「言い回しが似た課題」でしかない。"""
    result = estimate(
        (
            SimilarTask(
                task_version_id="a", title="A", score=0.9, method=SimilarityMethod.LEXICAL
            ),
            SimilarTask(
                task_version_id="b", title="B", score=0.8, method=SimilarityMethod.LEXICAL
            ),
        ),
        {"a": _stats("a", 40, 20), "b": _stats("b", 40, 20)},
        method=SimilarityMethod.LEXICAL,
    )
    assert "言い回しが似ているだけ" in result.summary()


def test_nothing_estimated_says_so_rather_than_showing_a_number() -> None:
    empty = DifficultyEstimate(reason="似た課題がありません")
    assert empty.verdict == "NOT_MEASURED"
    assert empty.label == "不明"
    assert "推定していません" in empty.summary()
