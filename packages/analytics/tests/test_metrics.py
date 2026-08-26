"""一致度指標の検算。

κ の実装は間違えやすく、間違えたまま「合格」と表示するのが最悪の失敗。
すべて手計算できる値を期待値に置いてある。
"""

from __future__ import annotations

import pytest

from aijudge_analytics import (
    Verdict,
    agreement_report,
    cohen_kappa,
    confusion_matrix,
    evaluate_gates,
    exact_agreement,
    miss_rate,
    overall,
    population_stdev,
    quadratic_weighted_kappa,
    review_rate,
)
from aijudge_analytics.gates import CriterionGate, Gates

# --------------------------------------------------------------------------
# Cohen's κ
# --------------------------------------------------------------------------


def test_cohen_kappa_matches_the_hand_computed_textbook_case() -> None:
    """2 値・50 件の教科書例。

    両者 yes 20、人間 yes/機械 no 5、人間 no/機械 yes 10、両者 no 15
    観測一致 = 35/50 = 0.70
    期待一致 = (25/50)(30/50) + (25/50)(20/50) = 0.30 + 0.20 = 0.50
    κ = (0.70 - 0.50) / (1 - 0.50) = 0.40
    """
    human = [1] * 20 + [1] * 5 + [0] * 10 + [0] * 15
    machine = [1] * 20 + [0] * 5 + [1] * 10 + [0] * 15
    assert cohen_kappa(human, machine) == pytest.approx(0.40)


def test_perfect_agreement_is_one() -> None:
    ratings = [0, 1, 2, 3, 2, 1]
    assert cohen_kappa(ratings, ratings) == pytest.approx(1.0)


def test_chance_level_agreement_is_about_zero() -> None:
    """偶然と同程度の一致は κ ≈ 0。完全一致率なら 0.5 に見えてしまう場面。"""
    human = [0, 0, 1, 1]
    machine = [0, 1, 0, 1]
    assert exact_agreement(human, machine) == pytest.approx(0.5)
    assert cohen_kappa(human, machine) == pytest.approx(0.0)


def test_everyone_agreeing_on_one_level_is_treated_as_full_agreement() -> None:
    """全員が同じ段階。偶然でも一致するので κ は本来定義できない。"""
    assert cohen_kappa([2, 2, 2], [2, 2, 2]) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# QWK
# --------------------------------------------------------------------------


def test_quadratic_weighted_kappa_matches_the_hand_computed_case() -> None:
    """3 段階・6 件。手計算:

    重み w_ij = (i-j)^2 / (3-1)^2
    観測 O: (0,0)=1 (0,1)=1 (1,1)=1 (1,2)=1 (2,2)=2
    分子 = 0.25*1 + 0.25*1 = 0.5
    周辺度数 人間=[2,2,2] 機械=[1,2,3]
    分母 = 2.0
    QWK = 1 - 0.5/2.0 = 0.75
    """
    human = [0, 0, 1, 1, 2, 2]
    machine = [0, 1, 1, 2, 2, 2]
    assert quadratic_weighted_kappa(human, machine, [0, 1, 2]) == pytest.approx(0.75)


def test_qwk_punishes_distant_disagreement_more_than_adjacent() -> None:
    """順序尺度で QWK を使う理由。κ ではこの差が出ない。"""
    human = [0, 1, 2, 3] * 3
    adjacent = [1, 2, 3, 2] * 3
    distant = [3, 3, 0, 0] * 3
    levels = [0, 1, 2, 3]

    assert quadratic_weighted_kappa(human, adjacent, levels) > quadratic_weighted_kappa(
        human, distant, levels
    )
    # Cohen's κ はどちらも「全部外した」として同じに見える。
    assert cohen_kappa(human, adjacent) == pytest.approx(cohen_kappa(human, distant))


def test_qwk_is_negative_when_the_machine_inverts_the_ordering() -> None:
    human = [0, 1, 2, 3]
    machine = [3, 2, 1, 0]
    assert quadratic_weighted_kappa(human, machine, [0, 1, 2, 3]) < 0.0


def test_a_rating_outside_the_declared_levels_is_an_error() -> None:
    with pytest.raises(ValueError, match="outside the declared levels"):
        confusion_matrix([0, 1], [0, 9], [0, 1, 2, 3])


# --------------------------------------------------------------------------
# レポート
# --------------------------------------------------------------------------


def test_an_empty_sample_reports_not_measurable_rather_than_zero() -> None:
    """測れていないことと、一致していないことは違う。"""
    report = agreement_report("readability", [], [], [0, 1, 2, 3])
    assert report.sample_size == 0
    assert not report.measurable
    assert report.cohen_kappa is None


def test_the_report_shows_which_way_the_machine_leans() -> None:
    """人間より機械が甘ければ mean_bias は負になる。"""
    human = [1, 1, 2, 2]
    machine = [2, 2, 3, 3]
    report = agreement_report("readability", human, machine, [0, 1, 2, 3])
    assert report.mean_bias == pytest.approx(-1.0)
    assert report.confusion[1][2] == 2


# --------------------------------------------------------------------------
# 見逃し率・レビュー率・ばらつき
# --------------------------------------------------------------------------


def test_miss_rate_counts_only_the_automatically_confirmed() -> None:
    """レビューに回した提出を人間が直すのは見逃しではない。"""
    auto = [True, True, True, False, False]
    changed = [False, True, False, True, True]
    assert miss_rate(auto, changed) == pytest.approx(1 / 3)


def test_miss_rate_is_unmeasurable_when_nothing_was_auto_confirmed() -> None:
    assert miss_rate([False, False], [True, True]) is None


def test_review_rate_is_the_fraction_not_auto_confirmed() -> None:
    assert review_rate([True, True, False, False]) == pytest.approx(0.5)


def test_population_stdev_of_a_single_repeat_is_zero() -> None:
    assert population_stdev([0.8]) == pytest.approx(0.0)
    assert population_stdev([0.8, 0.8, 0.8]) == pytest.approx(0.0)
    assert population_stdev([0.0, 1.0]) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# 合格判定 — ここが本題
# --------------------------------------------------------------------------


GATES = Gates(
    poc="PoC-1",
    min_sample_size=30,
    criteria={"readability": CriterionGate(min_cohen_kappa=0.65)},
    max_miss_rate=0.05,
)


def test_a_thin_sample_is_never_a_pass_however_good_it_looks() -> None:
    """3 件で κ = 1.0 でも合格ではない。ここを 2 値にすると自分を騙す。"""
    report = agreement_report("readability", [1, 2, 3], [1, 2, 3], [0, 1, 2, 3])
    checks = evaluate_gates(GATES, {"readability": report})

    assert [check.verdict for check in checks if "κ" in check.name] == [Verdict.NOT_MEASURED]
    assert "3/30" in next(check for check in checks if "κ" in check.name).detail
    assert overall(checks) is not Verdict.PASS


def test_a_missing_criterion_is_not_measured_rather_than_absent() -> None:
    checks = evaluate_gates(GATES, {})
    kappa_check = next(check for check in checks if "κ" in check.name)
    assert kappa_check.verdict is Verdict.NOT_MEASURED
    assert "0/30" in kappa_check.detail


def test_a_sufficient_sample_above_the_threshold_passes() -> None:
    ratings = ([0, 1, 2, 3] * 8)[:30]
    report = agreement_report("readability", ratings, ratings, [0, 1, 2, 3])
    checks = evaluate_gates(GATES, {"readability": report}, observed_miss_rate=0.0)
    assert overall(checks) is Verdict.PASS


def test_one_failing_check_fails_the_whole_gate() -> None:
    ratings = ([0, 1, 2, 3] * 8)[:30]
    report = agreement_report("readability", ratings, ratings, [0, 1, 2, 3])
    checks = evaluate_gates(GATES, {"readability": report}, observed_miss_rate=0.20)
    assert overall(checks) is Verdict.FAIL


def test_unmeasured_beats_pass_but_loses_to_fail() -> None:
    """FAIL > NOT_MEASURED > PASS の優先順位。"""
    ratings = ([0, 1, 2, 3] * 8)[:30]
    report = agreement_report("readability", ratings, ratings, [0, 1, 2, 3])
    # 見逃し率が測れていない
    assert overall(evaluate_gates(GATES, {"readability": report})) is Verdict.NOT_MEASURED
