"""書式だけが違う提出を、解けていない提出と区別する。

**判定は変えない。** 書式指定も課題の一部で、区切り方が違えば不正解のまま
（`normalize_output` の判断）。ここで固定するのは**なぜ落ちたかを言えること**
だけである。

言えないと何が起きるか。書式の食い違いは全ケースを同時に落とすので、
学習者に返るのは「5 件中 0 件が一致しました」だけになり、解けていない提出と
見分けが付かない。`_common_error` が全件共通の理由を伝えようとしているのと
同じ意図だが、あちらは stderr かシグナルがある場合しか働かず、正常終了する
書式違いは漏れる。
"""

from __future__ import annotations

from aijudge_eval_code_test_runner import format_only_mismatch, normalize_output


def _pair(expected: str, actual: str):
    return normalize_output(expected), normalize_output(actual)


def test_a_comma_instead_of_a_space_is_named() -> None:
    """課題文が「1つの半角空白文字で区切られた一行に出力する」と書いている
    のに対し、値は合っていて区切りだけが違う場合。
    """
    note = format_only_mismatch(*_pair("3 1 2", "3,1,2"))
    assert note is not None
    assert "区切り方" in note


def test_extra_spacing_is_named() -> None:
    note = format_only_mismatch(*_pair("3 1 2", "3   1   2"))
    assert note is not None
    assert "区切り方" in note


def test_a_line_break_in_the_wrong_place_is_named() -> None:
    note = format_only_mismatch(*_pair("3 1 2", "3\n1\n2"))
    assert note is not None
    assert "改行の位置" in note


def test_a_different_number_format_is_named() -> None:
    note = format_only_mismatch(*_pair("3 1 2.0", "3 1 2.000"))
    assert note is not None
    assert "数値" in note


def test_the_trailing_newline_was_never_a_mismatch() -> None:
    """末尾の改行は `normalize_output` が既に吸収している。

    診断の対象ですらない（一致するので、そもそも落ちない）。
    """
    expected, actual = _pair("3 1 2\n", "3 1 2")
    assert expected == actual
    assert format_only_mismatch(expected, actual) is None


def test_a_wrong_value_is_not_called_a_format_problem() -> None:
    """**間違いを書式のせいにしない。** 直す先を取り違えさせる。"""
    assert format_only_mismatch(*_pair("3 1 2", "3 1 5")) is None


def test_a_missing_value_is_not_a_format_problem() -> None:
    assert format_only_mismatch(*_pair("3 1 2", "3 1")) is None


def test_no_output_at_all_is_not_a_format_problem() -> None:
    assert format_only_mismatch(*_pair("3 1 2", "")) is None


def test_a_real_precision_difference_is_not_a_format_problem() -> None:
    """有効数字が課題の一部である科目がある。値が違えば書式の話ではない。"""
    assert format_only_mismatch(*_pair("0.333", "0.3333")) is None
