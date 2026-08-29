"""重複検出の規則を固定する（S2、設計方針 §5）。

固定したいのは 4 つ。

写しは捉える     字面でも同じ課題文は 1.0 になる。
言い換えは捉えない 字面の限界。**それを結果に明記する**（安全の証拠に読ませない）。
比較していない ≠ 重複なし 既存が 0 件のとき「無い」と言わない。
判定ではない     近いことは欠陥ではない。捨てるのは教員（P5）。
"""

from __future__ import annotations

import pytest

from aijudge_authoring.similarity import (
    DuplicateReport,
    SimilarityMethod,
    cosine,
    lexical,
    rank,
)

COPY = "2 つの整数を読み、その和を出力しなさい。"
PARAPHRASE = "ふたつの数値を入力し、合計を表示せよ。"
OTHER = "行列の積を求めるプログラムを書きなさい。"


def test_a_copy_is_found_by_the_lexical_measure() -> None:
    assert lexical(COPY, COPY) == 1.0


def test_a_paraphrase_is_not_found_by_the_lexical_measure() -> None:
    """**これが字面の限界である。** 見つからなかったことは安全の証拠ではない。"""
    assert lexical(COPY, PARAPHRASE) < 0.2


def test_unrelated_statements_score_low() -> None:
    assert lexical(COPY, OTHER) < 0.2


def test_cosine_refuses_vectors_of_different_size() -> None:
    """次元が違う＝別のモデルの出力。**混ぜると無関係な課題が似ていることになる。**"""
    with pytest.raises(ValueError):
        cosine((1.0, 0.0), (1.0, 0.0, 0.0))


def test_cosine_is_one_for_the_same_direction() -> None:
    assert cosine((1.0, 2.0), (2.0, 4.0)) == pytest.approx(1.0)


def test_nothing_compared_is_not_nothing_found() -> None:
    """**「比較していない」と「近いものが無い」を混ぜない。**"""
    report = DuplicateReport(method=SimilarityMethod.LEXICAL, compared=0)
    assert not report.checked
    assert "検査していません" in report.summary()


def test_the_measure_used_is_always_stated() -> None:
    report = rank(
        {"tsv_a": ("既存の課題", 0.95)},
        method=SimilarityMethod.LEXICAL,
    )
    text = report.summary()
    assert "字面のみ" in text
    assert "言い換えた重複は見つかりません" in text


def test_being_close_is_material_not_a_verdict() -> None:
    """同じ単元なら似るのが当然。捨てるかどうかは教員が決める（P5）。"""
    report = rank({"tsv_a": ("第 3 回の練習", 0.92)}, method=SimilarityMethod.EMBEDDING)
    assert report.too_close
    assert "教員が決めます" in report.summary()


def test_the_nearest_are_ordered_and_capped() -> None:
    report = rank(
        {f"tsv_{i}": (f"課題{i}", i / 10) for i in range(9)},
        method=SimilarityMethod.EMBEDDING,
        top=3,
    )
    assert [item.score for item in report.nearest] == [0.8, 0.7, 0.6]
    assert report.compared == 9
