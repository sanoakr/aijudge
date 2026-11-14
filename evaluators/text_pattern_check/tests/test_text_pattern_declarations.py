"""宣言の誤りは、黙って緩い判定に変えずに評価器の失敗にする（2026-11-14）。

綴り違い 1 つで照合が「空でない行があれば満たす」に落ちると、誰の提出でも
満点で確定し、記録からは誤りが見えない。失敗にすれば観点は未採点のまま人に回る。
"""

from __future__ import annotations

import pytest
from aijudge_eval_text_pattern_check import PatternSpec, satisfies
from test_text_pattern_check import BODY, NICKNAME, TextPatternCheck, _case, _request

from aijudge_core import EvaluatorStatus

OTHER_LEARNER = "y999999@mail.example.ac.jp"


def _outcome(*cases):
    return TextPatternCheck().evaluate(_request(cases=cases, learner_reference=OTHER_LEARNER))


def test_an_unknown_expect_fails_the_evaluator() -> None:
    misspelt = _case(
        "受講者欄", criterion="nickname", expect="learner_referense", line_matches="認定証"
    )
    outcome = _outcome(misspelt)
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.scores == ()
    assert "expect" in (outcome.error or "")


def test_an_unknown_expect_is_refused_by_the_spec_itself() -> None:
    with pytest.raises(ValueError):
        PatternSpec(name="受講者欄", expect="student_id", line_matches="認定証")


def test_an_unknown_payload_key_fails_the_evaluator() -> None:
    misspelt = _case("修了", criterion="nickname", patern="修了")
    outcome = _outcome(misspelt)
    assert outcome.status is EvaluatorStatus.FAILED
    assert "patern" in (outcome.error or "")


def test_an_unreadable_regular_expression_fails_the_evaluator() -> None:
    broken = _case("修了", criterion="nickname", pattern="修了(")
    assert _outcome(broken).status is EvaluatorStatus.FAILED


@pytest.mark.parametrize("field", ["line_matches", "distinct_by", "section", "ignore_lines"])
def test_every_regular_expression_field_is_checked(field: str) -> None:
    payload = {"pattern": "修了", field: "("}
    broken = _case("修了", criterion="nickname", **payload)
    assert _outcome(broken).status is EvaluatorStatus.FAILED


def test_a_declaration_that_asks_nothing_fails_the_evaluator() -> None:
    empty = _case("何か", criterion="nickname", required=True)
    assert _outcome(empty).status is EvaluatorStatus.FAILED


def test_a_section_with_min_chars_and_no_pattern_is_still_a_declaration() -> None:
    """アンケートの節の判定（`section`・`min_chars`）は、pattern 無しで正しい宣言。"""
    spec = PatternSpec(name="回答", section=r"1\\.", min_chars=2)
    assert spec.section


def test_the_correct_spelling_still_checks_the_learner() -> None:
    """正しい宣言は今までどおり本人を照合する（他人の番号では満たさない）。"""
    assert not satisfies(PatternSpec(**_spec_payload(NICKNAME)), BODY, OTHER_LEARNER)
    assert satisfies(PatternSpec(**_spec_payload(NICKNAME)), BODY, "y240040@mail.example.ac.jp")


def _spec_payload(case) -> dict:
    return {"name": case.name, **case.payload}
