"""評価器の一覧を上書きしたコースに、雛形の後からの追加が届いたとき（2026-10-11）。

prog2 のコースは評価器の一覧を `[code_test_runner, text_pattern_check]` で上書きして
いた。運用の雛形に `c_indent_check` とその設定を足したら、重ねた結果が「走らせない
評価器に設定がある」で検査に落ち、そのコースの採点が 6 時間全件失敗した。

固定したいのは 2 つ。

- 雛形から受け継いだ、コースが走らせない評価器の設定は外れる（採点は止まらない）
- コース自身が書いた設定は外さない（保存の検査で教員に返す）
"""

from __future__ import annotations

import pytest

from aijudge_grading import OverrideError, SubjectProfile, default_registry, effective_profile

BASE = SubjectProfile(
    name="cs_lang_c_intro",
    deterministic=("text_pattern_check", "code_test_runner", "c_indent_check"),
    evaluator_options={
        "code_test_runner": {"language": "c", "case_timeout_seconds": 2.0},
        "c_indent_check": {"partial_max_ratio": 0.2},
    },
)
FROZEN = {"deterministic": ["code_test_runner", "text_pattern_check"]}


def test_a_frozen_list_does_not_break_when_the_template_adds_an_evaluator() -> None:
    applied = effective_profile(BASE, FROZEN, default_registry())
    assert applied.deterministic == ("code_test_runner", "text_pattern_check")
    assert "c_indent_check" not in applied.evaluator_options
    # 走る評価器の設定は残る。
    assert applied.evaluator_options["code_test_runner"]["case_timeout_seconds"] == 2.0


def test_options_the_course_itself_wrote_for_an_unused_evaluator_are_refused() -> None:
    overrides = {**FROZEN, "evaluator_options": {"c_indent_check": {"partial_max_ratio": 0.1}}}
    with pytest.raises(OverrideError, match="c_indent_check"):
        effective_profile(BASE, overrides, default_registry())


def test_a_course_without_a_list_keeps_every_option() -> None:
    applied = effective_profile(BASE, {"timeout_seconds": 60.0}, default_registry())
    assert applied.evaluator_options["c_indent_check"] == {"partial_max_ratio": 0.2}
