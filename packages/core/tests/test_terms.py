"""学期の語彙（#167）。

ここで守っているのは表示の綺麗さではない。**コースの同一性は
(テナント, コード, 学期) で、ID はそこから導かれる** ── `2025-後期` と
`2025後期` は同じ授業のつもりで別のコースになり、しかも学期は作成後に
変えられない（変えることは別のコースを作ることである）。
"""

from __future__ import annotations

from datetime import date

import pytest

from aijudge_core import (
    DIVISIONS,
    academic_year,
    format_term,
    is_valid_term,
    offered_years,
    parse_term,
    term_sort_key,
)


def test_the_canonical_form_is_the_one_already_in_the_data() -> None:
    """**既存データと同じ形。** 形を変えると既存コースの ID を作り直すことになる。"""
    assert format_term(2026, "前期") == "2026-前期"
    assert parse_term("2025-後期") == (2025, "後期")


def test_every_division_round_trips() -> None:
    for division in DIVISIONS:
        assert parse_term(format_term(2026, division)) == (2026, division)


@pytest.mark.parametrize(
    "term",
    ["2026後期", "2026-春", "26-前期", "前期", "2026-", "", "2026-前期-2"],
)
def test_a_malformed_term_is_refused(term: str) -> None:
    assert not is_valid_term(term)
    with pytest.raises(ValueError):
        parse_term(term)


def test_an_unknown_division_is_refused() -> None:
    """**区分は増やせない。** 増やせると `春学期` と `前期` が並ぶ。"""
    with pytest.raises(ValueError):
        format_term(2026, "春学期")


def test_the_year_is_the_academic_year() -> None:
    """1〜3 月は前の年の年度である（年度は 4 月に始まる）。"""
    assert academic_year(date(2026, 4, 1)) == 2026
    assert academic_year(date(2026, 9, 8)) == 2026
    assert academic_year(date(2027, 3, 31)) == 2026


def test_the_years_offered_start_at_this_year() -> None:
    """今年度から 3 つ。**過去は出さない** ── コースを遡って作る操作は無い。"""
    assert offered_years(date(2026, 9, 8)) == (2026, 2027, 2028)
    assert offered_years(date(2026, 2, 1)) == (2025, 2026, 2027)


def test_terms_sort_chronologically_not_alphabetically() -> None:
    """**文字列順ではない。**

    `前期`／`後期` はたまたま文字コード順が時系列と一致するが、`1Q`〜`4Q` が
    混ざると一致しない（`1Q` < `前期`）。
    """
    terms = ["2026-4Q", "2025-後期", "2026-前期", "2026-1Q", "2026-集中", "2026-通年"]

    assert sorted(terms, key=term_sort_key) == [
        "2025-後期",
        "2026-前期",
        "2026-1Q",
        "2026-4Q",
        "2026-通年",
        "2026-集中",
    ]


def test_all_year_round_and_intensive_come_last() -> None:
    """どちらも特定の四半期に属さないので最後に置く（教員の指定）。"""
    assert DIVISIONS[-2:] == ("通年", "集中")


def test_a_value_from_the_free_text_era_sorts_last_without_crashing() -> None:
    """**落ちない。** 自由入力だった時代の値が残っていても一覧は出る。

    黙って前期の隣に混ぜず最後に置くのは、並びがおかしいことが見える方が
    よいため。
    """
    terms = ["2026-前期", "2025年度後期", "集中講義"]

    assert sorted(terms, key=term_sort_key) == ["2026-前期", "2025年度後期", "集中講義"]
