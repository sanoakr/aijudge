"""課題ごとの実行上限の重ね方（#491・科目 ← コース ← 課題）。"""

from __future__ import annotations

from aijudge_grading import SubjectProfile, effective_profile, with_task_case_timeout
from aijudge_grading.overrides import CASE_TIMEOUT_OPTION

BASE = SubjectProfile(
    name="cs_lang_c_intro",
    deterministic=("code_test_runner", "network_test_runner"),
    evaluator_options={
        "code_test_runner": {CASE_TIMEOUT_OPTION: 2.0, "compile_timeout_seconds": 30.0},
        "network_test_runner": {CASE_TIMEOUT_OPTION: 10.0},
    },
)


def test_no_limit_on_the_task_leaves_the_profile_alone() -> None:
    assert with_task_case_timeout(BASE, None) is BASE


def test_the_tasks_limit_reaches_every_deterministic_evaluator() -> None:
    applied = with_task_case_timeout(BASE, 30.0)
    assert applied.evaluator_options["code_test_runner"][CASE_TIMEOUT_OPTION] == 30.0
    assert applied.evaluator_options["network_test_runner"][CASE_TIMEOUT_OPTION] == 30.0


def test_other_options_are_kept() -> None:
    applied = with_task_case_timeout(BASE, 30.0)
    assert applied.evaluator_options["code_test_runner"]["compile_timeout_seconds"] == 30.0


def test_the_task_wins_over_the_course() -> None:
    course = effective_profile(
        BASE, {"evaluator_options": {"code_test_runner": {CASE_TIMEOUT_OPTION: 7.0}}}
    )
    applied = with_task_case_timeout(course, 20.0)
    assert applied.evaluator_options["code_test_runner"][CASE_TIMEOUT_OPTION] == 20.0


def test_ai_evaluators_are_not_given_the_option() -> None:
    """AI 評価器の予算（`timeout_seconds`）とは別物。AI には渡さない。"""
    profile = BASE.model_copy(update={"ai_evaluators": ("rubric_ai_judge",)})
    applied = with_task_case_timeout(profile, 30.0)
    assert "rubric_ai_judge" not in applied.evaluator_options
    assert applied.timeout_seconds == profile.timeout_seconds
