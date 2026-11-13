"""`section`・`ignore_lines`・`min_chars` ── 見出しの下に回答があるか。

network の survey（2026-11-13）を模す。質問文はテンプレートに最初から
入っているので数えない。何も無ければ未達で確定、十分に書いてあれば達成で
確定、その間（1 字だけ・記号だけ・同じ文字の繰り返し・見出しが無い）は
確定させずに人のレビューへ回す。
"""

from __future__ import annotations

from aijudge_eval_text_pattern_check import (
    EVALUATOR_ID,
    UNDECIDED_CONFIDENCE,
    PatternSpec,
    Presence,
    TextPatternCheck,
    counted_chars,
    presence,
    section_lines,
)
from test_text_pattern_check import _case, _criterion, _request

from aijudge_core import ReviewPolicy

TEMPLATE = """## 1. よかった点
> aiJudge を使って、よかった・助かったと感じたこと

{good}

## 2. 困った点
> 困ったこと、使いにくかったこと

{trouble}
"""

IGNORE_QUESTION = r"^\s*>"


def _body(good: str = "", trouble: str = "") -> str:
    return TEMPLATE.format(good=good, trouble=trouble)


def _spec(heading: str = r"1\.\s*よかった点", **extra: object) -> PatternSpec:
    fields: dict[str, object] = {
        "name": "回答",
        "section": heading,
        "ignore_lines": IGNORE_QUESTION,
        "min_chars": 2,
    }
    fields.update(extra)
    return PatternSpec.model_validate(fields)


GOOD = _case(
    "回答",
    criterion="good",
    section=r"1\.\s*よかった点",
    ignore_lines=IGNORE_QUESTION,
    min_chars=2,
)


def _score(body: str):
    outcome = TextPatternCheck().evaluate(
        _request(cases=(GOOD,), criteria=(_criterion("good"),), body=body)
    )
    return outcome.scores[0], outcome


# --------------------------------------------------------------------------
# 範囲の取り方
# --------------------------------------------------------------------------


def test_the_section_stops_at_the_next_heading() -> None:
    lines = section_lines(_body("すぐ結果が出る", "なし"), r"1\.")
    assert lines is not None
    assert "すぐ結果が出る" in lines
    assert "なし" not in lines


def test_a_missing_heading_is_not_the_same_as_an_empty_section() -> None:
    assert section_lines("## 別の見出し\n本文", r"1\.\s*よかった点") is None
    assert section_lines("## 1. よかった点\n", r"1\.\s*よかった点") == []


def test_html_comments_are_not_read() -> None:
    body = "## 1. よかった点\n<!-- ここに\n書いてください -->\n"
    assert presence(_spec(), body, None) is Presence.UNMET


def test_a_heading_like_line_without_hashes_does_not_open_the_section() -> None:
    """本文中の「1. よかった点」という文は見出しではない。"""
    body = "1. よかった点 はとくに無い\n## 1. よかった点\n"
    assert section_lines(body, r"1\.\s*よかった点") == []


# --------------------------------------------------------------------------
# 字数の数え方
# --------------------------------------------------------------------------


def test_symbols_and_spaces_are_not_counted() -> None:
    assert counted_chars("！？ 。、 ...") == 0
    assert counted_chars("特になし。") == 4


def test_one_repeated_character_counts_as_nothing() -> None:
    assert counted_chars("ああああああ") == 0
    assert counted_chars("aaaa") == 0


def test_a_short_real_answer_counts() -> None:
    """「なし」は回答である（問題文が「特にない」も回答と書いている）。"""
    assert counted_chars("なし") == 2


# --------------------------------------------------------------------------
# 3 通りの判定
# --------------------------------------------------------------------------


def test_only_the_question_left_in_place_is_unmet_and_conclusive() -> None:
    score, _ = _score(_body())
    assert score.level == 0
    assert score.conclusive is True
    assert score.confidence == 1.0


def test_a_real_answer_is_met_and_conclusive() -> None:
    score, _ = _score(_body("すぐ結果が出るのが助かった"))
    assert score.level == 1
    assert score.conclusive is True


def test_a_short_none_answer_is_met() -> None:
    score, _ = _score(_body("特にない"))
    assert score.level == 1
    assert score.conclusive is True


def test_a_single_character_is_left_to_a_human() -> None:
    score, outcome = _score(_body("あ"))
    assert score.level == 0
    assert score.conclusive is False
    assert score.confidence == UNDECIDED_CONFIDENCE
    assert "人の確認" in score.rationale
    assert outcome.raw_output["undecided"] == {"good": ["回答"]}


def test_symbols_only_are_left_to_a_human() -> None:
    score, _ = _score(_body("。。。！！"))
    assert score.conclusive is False


def test_a_repeated_character_is_left_to_a_human() -> None:
    score, _ = _score(_body("ああああああああ"))
    assert score.conclusive is False


def test_a_deleted_heading_is_left_to_a_human() -> None:
    """見出しを消して回答を書いた提出を「何も書いていない」と確定させない。"""
    score, _ = _score("よかった点: すぐ結果が出る")
    assert score.conclusive is False
    assert score.level == 0


def test_an_undecided_score_routes_to_review_under_the_default_policy() -> None:
    score, _ = _score(_body("あ"))
    assert ReviewPolicy().requires_review((score,), total_ratio=0.0) is True


def test_a_conclusive_score_does_not_route_to_review() -> None:
    score, _ = _score(_body("すぐ結果が出る"))
    assert ReviewPolicy().requires_review((score,), total_ratio=1.0) is False


# --------------------------------------------------------------------------
# 今までの宣言は変わらない
# --------------------------------------------------------------------------


def test_without_min_chars_any_written_line_still_counts() -> None:
    spec = PatternSpec(name="x", section=r"1\.", ignore_lines=IGNORE_QUESTION)
    assert presence(spec, _body("あ"), None) is Presence.MET


def test_a_pattern_inside_a_section_is_still_conclusive() -> None:
    case = _case("語", criterion="good", section=r"2\.", pattern="なし")
    outcome = TextPatternCheck().evaluate(
        _request(cases=(case,), criteria=(_criterion("good"),), body=_body("なし", "ある"))
    )
    assert outcome.scores[0].level == 0
    assert outcome.scores[0].conclusive is True


def test_the_evaluator_id_is_unchanged() -> None:
    assert EVALUATOR_ID == "text_pattern_check"
