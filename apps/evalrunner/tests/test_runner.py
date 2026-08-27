"""測定の足場そのものを検証する。

測る道具が壊れていると、壊れていることに気づけない。

**採点は一切走らせない。** 測定は記録済みの観測レコードを読むだけなので、
テストも観測を直接組み立てて渡す（ADR 0007）。LLM もコンパイラも要らない。
これ自体が「測定が採点に依存していない」ことの証拠になっている。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aijudge_analytics import Gates, Verdict
from aijudge_analytics.gates import CriterionGate
from aijudge_evalrunner import ObservationSetError, load_observations, measure
from aijudge_evalrunner.cli import EXIT_FAIL, EXIT_NOT_MEASURED, EXIT_PASS, main, render
from aijudge_observation import Observation

REPO_ROOT = Path(__file__).resolve().parents[3]
GATES_PATH = REPO_ROOT / "evals" / "gates.yaml"

LEVELS = (0, 1, 2, 3)


def observation(
    submission: str,
    *,
    code: str = "readability",
    human: int | None = 1,
    machine: int | None = 1,
    blind: bool = True,
    conclusive: bool = False,
    unscored: bool = False,
    auto_confirmed: bool = True,
    changed: bool | None = False,
    graded: bool = True,
) -> Observation:
    return Observation(
        subject_profile="cs_intro_c",
        task_name="example-task",
        submission=submission,
        criterion_code=code,
        levels=LEVELS,
        machine_level=machine,
        machine_confidence=1.0 if machine is not None else None,
        conclusive=conclusive,
        unscored=unscored,
        grading_run_id="grn_test" if graded else None,
        human_level=human,
        blind=blind,
        marker="tester" if human is not None else None,
        auto_confirmed=auto_confirmed,
        machine_corrected=changed,
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def _gates(**overrides: object) -> Gates:
    base: dict[str, object] = {
        "poc": "test",
        "min_sample_size": 2,
        "criteria": {"readability": CriterionGate(min_cohen_kappa=0.5)},
    }
    base.update(overrides)
    return Gates.model_validate(base)


def _write(root: Path, submission: str, observations: list[Observation]) -> None:
    folder = root / "cs_intro_c" / "example-task" / "observations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{Path(submission).stem}.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in observations], ensure_ascii=False),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# 測定は採点しない
# --------------------------------------------------------------------------


def test_measuring_needs_nothing_but_observations() -> None:
    """課題定義もサンドボックスも LLM も無い状態で測定が完結すること。"""
    report = measure(
        [observation("s001.c", human=1, machine=1), observation("s002.c", human=2, machine=2)],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.agreement["readability"].sample_size == 2
    assert report.agreement["readability"].cohen_kappa == 1.0
    assert report.verdict is Verdict.PASS


def test_the_levels_come_from_the_record_not_from_a_task_definition() -> None:
    """段階の集合は観測に焼き込まれている。

    課題定義から引き直す設計だと、引けなかったときに既定値へ黙って落ちて
    QWK の重み行列が狂う。ここでは 5 段階の観測を渡して、その通りに
    扱われることを確かめる。
    """
    wide = [
        observation("s001.c", human=0, machine=0).model_copy(update={"levels": (0, 1, 2, 3, 4)}),
        observation("s002.c", human=4, machine=4).model_copy(update={"levels": (0, 1, 2, 3, 4)}),
    ]
    report = measure(wide, gates=_gates(), subject_profile="cs_intro_c")
    assert report.agreement["readability"].levels == (0, 1, 2, 3, 4)


def test_conflicting_level_sets_are_refused() -> None:
    """同じ観点で段階数が違うのは構成の誤り。黙って片方に寄せない。"""
    mixed = [
        observation("s001.c"),
        observation("s002.c").model_copy(update={"levels": (0, 1, 2, 3, 4)}),
    ]
    with pytest.raises(ValueError, match="conflicting level sets"):
        measure(mixed, gates=_gates(), subject_profile="cs_intro_c")


# --------------------------------------------------------------------------
# 標本の選別 — 数えてはいけないものを数えない
# --------------------------------------------------------------------------


def test_non_blind_marks_are_excluded() -> None:
    """AI を見てから付けた採点は正解データにならない（ADR 0005）。"""
    report = measure(
        [
            observation("s001.c", blind=True),
            observation("s002.c", blind=False),
            observation("s003.c", blind=True),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.agreement["readability"].sample_size == 2
    assert report.excluded["blind でない教員採点"] == 1


def test_deterministic_criteria_are_excluded() -> None:
    """決定的評価が確定させた観点は AI の精度ではない。"""
    report = measure(
        [
            observation("s001.c", code="correctness", conclusive=True),
            observation("s002.c", code="correctness", conclusive=True),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert "correctness" not in report.agreement
    assert report.excluded["決定的評価が確定（AI は関与しない）"] == 2


def test_unscored_criteria_are_counted_as_neither_agreement_nor_disagreement() -> None:
    report = measure(
        [observation("s001.c", unscored=True), observation("s002.c")],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.agreement["readability"].sample_size == 1
    assert report.excluded["採点できなかった観点"] == 1


def test_submissions_without_an_instructor_mark_are_excluded() -> None:
    """採点済みだが教員が見ていない提出。運用の大多数がこれ。"""
    report = measure(
        [observation("s001.c", human=None, blind=False, changed=None)],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert not report.agreement
    assert report.excluded["教員採点がない"] == 1


def test_excluded_observations_are_reported_not_hidden() -> None:
    """黙って標本を減らさない（ADR 0005）。"""
    report = measure(
        [observation("s001.c", blind=False), observation("s002.c", unscored=True)],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert sum(report.excluded.values()) == 2
    assert "一致度の標本から外した観測" in render(report)


# --------------------------------------------------------------------------
# 運用の指標
# --------------------------------------------------------------------------


def test_the_review_rate_counts_submissions_not_criteria() -> None:
    """1 提出に観点が複数あっても、レビュー行き率は提出単位で数える。"""
    report = measure(
        [
            observation("s001.c", code="correctness", auto_confirmed=True),
            observation("s001.c", code="readability", auto_confirmed=True),
            observation("s002.c", code="correctness", auto_confirmed=False),
            observation("s002.c", code="readability", auto_confirmed=False),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.submission_count == 2
    assert report.observed_review_rate == 0.5


def test_the_miss_rate_uses_what_the_instructor_changed_after_seeing_the_ai() -> None:
    """見逃し = 自動確定したのに、AI を見た教員が直した提出。"""
    report = measure(
        [
            observation("s001.c", auto_confirmed=True, changed=True),
            observation("s002.c", auto_confirmed=True, changed=False),
            observation("s003.c", auto_confirmed=True, changed=False),
            observation("s004.c", auto_confirmed=True, changed=False),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.observed_miss_rate == 0.25


def test_unfinalized_submissions_are_not_in_the_miss_rate_denominator() -> None:
    """まだ教員が見ていない提出は「直さなかった」ではない。"""
    report = measure(
        [
            observation("s001.c", auto_confirmed=True, changed=True),
            observation("s002.c", auto_confirmed=True, changed=None),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.observed_miss_rate == 1.0


def test_no_observations_at_all_is_not_measured() -> None:
    report = measure([], gates=_gates(), subject_profile="cs_intro_c")
    assert report.verdict is Verdict.NOT_MEASURED
    assert report.observation_count == 0


# --------------------------------------------------------------------------
# 「測れていない」を「合格」に丸めない（ADR 0005）
# --------------------------------------------------------------------------


def test_a_small_sample_is_not_measured_however_good_the_numbers_are() -> None:
    report = measure(
        [observation("s001.c", human=2, machine=2)],
        gates=_gates(min_sample_size=30),
        subject_profile="cs_intro_c",
    )
    assert report.agreement["readability"].cohen_kappa == 1.0
    assert report.verdict is Verdict.NOT_MEASURED
    assert "1/30 件" in render(report)


def test_falling_short_of_the_threshold_fails() -> None:
    report = measure(
        [
            observation("s001.c", human=1, machine=3),
            observation("s002.c", human=2, machine=0),
            observation("s003.c", human=3, machine=1),
        ],
        gates=_gates(),
        subject_profile="cs_intro_c",
    )
    assert report.verdict is Verdict.FAIL


# --------------------------------------------------------------------------
# ファイルからの読み込み
# --------------------------------------------------------------------------


def test_observations_are_read_from_the_tree(tmp_path: Path) -> None:
    _write(tmp_path, "s001.c", [observation("s001.c"), observation("s001.c", code="correctness")])
    loaded = load_observations(tmp_path, "cs_intro_c")
    assert len(loaded) == 2


def test_another_subject_is_not_mixed_in(tmp_path: Path) -> None:
    _write(tmp_path, "s001.c", [observation("s001.c")])
    assert load_observations(tmp_path, "math_calculus") == ()


def test_a_broken_observation_file_raises(tmp_path: Path) -> None:
    """黙って飛ばすと、標本が減ったことに気づかないまま κ を見る。"""
    folder = tmp_path / "cs_intro_c" / "example-task" / "observations"
    folder.mkdir(parents=True)
    (folder / "s001.json").write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(ObservationSetError, match="list of observations"):
        load_observations(tmp_path, "cs_intro_c")


def test_a_missing_root_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_observations(tmp_path / "nope") == ()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_the_cli_exits_2_when_there_is_nothing_to_measure(tmp_path: Path, capsys) -> None:
    """CI が「測れていない」を「合格」と取り違えないため（ADR 0005）。"""
    code = main(["--golden", str(tmp_path), "--gates", str(GATES_PATH)])
    assert code == EXIT_NOT_MEASURED
    assert "観測レコードがありません" in capsys.readouterr().err


def test_the_cli_says_grading_still_works_without_measurement(tmp_path: Path, capsys) -> None:
    """データが無いのは運用の失敗ではない（ADR 0007）。"""
    _write(tmp_path, "s001.c", [observation("s001.c", human=None, blind=False, changed=None)])
    main(["--golden", str(tmp_path), "--gates", str(GATES_PATH)])
    assert "採点運用は成立している" in capsys.readouterr().out


def test_the_cli_reports_pass_and_fail_from_the_gates(tmp_path: Path) -> None:
    gates = tmp_path / "gates.yaml"
    gates.write_text(
        yaml.safe_dump(
            {
                "poc": "test",
                "min_sample_size": 2,
                "criteria": {"readability": {"min_cohen_kappa": 0.5}},
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "agree"
    _write(root, "s001.c", [observation("s001.c", human=1, machine=1)])
    _write(root, "s002.c", [observation("s002.c", human=2, machine=2)])
    assert main(["--golden", str(root), "--gates", str(gates)]) == EXIT_PASS

    root = tmp_path / "disagree"
    _write(root, "s001.c", [observation("s001.c", human=1, machine=3)])
    _write(root, "s002.c", [observation("s002.c", human=2, machine=0)])
    assert main(["--golden", str(root), "--gates", str(gates)]) == EXIT_FAIL


def test_the_report_says_it_did_not_grade(tmp_path: Path) -> None:
    report = measure([observation("s001.c")], gates=_gates(), subject_profile="cs_intro_c")
    assert "このコマンドは採点しません" in render(report)
