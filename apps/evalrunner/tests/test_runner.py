"""測定の足場そのものを検証する。

測る道具が壊れていると、壊れていることに気づけない。
LLM は呼ばず、スクリプト応答で配線だけを確かめる。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_analytics import Gates, Verdict
from aijudge_analytics.gates import CriterionGate
from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_evalrunner import GoldenSetError, load_golden, run_evaluation
from aijudge_evalrunner.cli import EXIT_NOT_MEASURED, main, render
from aijudge_evalrunner.runner import stored_run
from aijudge_grading import EvaluatorRegistry, GradingPipeline, load_profile
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_GOLDEN = REPO_ROOT / "evals" / "golden"
PROFILES = REPO_ROOT / "subjects"
GATES_PATH = REPO_ROOT / "evals" / "gates.yaml"

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

# 読みやすさを段階 1 と判定する応答。例のマークも 1 なので一致する。
VERDICT_1 = (
    '{"level": 1, "evidence": [{"start_line": 5, "end_line": 5, '
    '"quote": "int b = 0, c = 0, d = 0;"}], "rationale": "変数名から役割が読み取れません。"}'
)
VERDICT_3 = (
    '{"level": 3, "evidence": [{"start_line": 5, "end_line": 5, '
    '"quote": "int b = 0, c = 0, d = 0;"}], "rationale": "明快です。"}'
)


# 科目プロファイルは自己一貫性のサンプル数を 3 にしている。テストは
# プロファイルを差し替えず本番と同じ設定で走らせるので、応答も 3 回ぶん要る。
PROFILE_SAMPLES = 3


def _registry(responses: list[str], *, repeat: bool = True) -> EvaluatorRegistry:
    scripted = [r for r in responses for _ in range(PROFILE_SAMPLES)] if repeat else responses
    registry = EvaluatorRegistry().load_installed()
    registry.replace(RubricAiJudge(LlmGateway(ScriptedProvider(scripted)), model="stub"))
    return registry


def _gates(**overrides) -> Gates:
    defaults = dict(
        poc="test",
        min_sample_size=1,
        criteria={"readability": CriterionGate(min_cohen_kappa=0.65)},
        max_miss_rate=0.05,
    )
    return Gates(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# ゴールデンセットの読み込み
# --------------------------------------------------------------------------


def test_the_example_golden_set_loads() -> None:
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    assert len(items) == 1
    item = items[0]
    assert item.key == "example-task/s001.c"
    assert item.mark.marks == {"correctness": 3, "readability": 1}
    assert item.mark.blind


def test_non_blind_marks_are_excluded_by_default(tmp_path: Path) -> None:
    """AI の出力を見てから付けた採点は正解データにならない。"""
    shutil.copytree(EXAMPLE_GOLDEN, tmp_path / "golden")
    mark = tmp_path / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.yaml"
    mark.write_text(mark.read_text().replace("blind: true", "blind: false"), encoding="utf-8")

    assert load_golden(tmp_path / "golden", "cs_intro_c") == ()
    assert len(load_golden(tmp_path / "golden", "cs_intro_c", blind_only=False)) == 1


def test_a_mark_pointing_at_a_missing_submission_is_an_error(tmp_path: Path) -> None:
    """黙って飛ばすと標本が減ったことに気づかないまま κ を見ることになる。"""
    shutil.copytree(EXAMPLE_GOLDEN, tmp_path / "golden")
    (tmp_path / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.c").unlink()

    with pytest.raises(GoldenSetError, match=r"s001\.c"):
        load_golden(tmp_path / "golden", "cs_intro_c")


def test_a_task_without_a_definition_is_an_error(tmp_path: Path) -> None:
    broken = tmp_path / "golden" / "cs_intro_c" / "no-task" / "marks"
    broken.mkdir(parents=True)
    with pytest.raises(GoldenSetError, match="no task/ directory"):
        load_golden(tmp_path / "golden", "cs_intro_c")


def test_a_missing_golden_directory_yields_nothing_rather_than_raising() -> None:
    assert load_golden(Path("/nonexistent/golden"), "cs_intro_c") == ()


# --------------------------------------------------------------------------
# 測定
# --------------------------------------------------------------------------


@needs_c_compiler
def test_agreement_is_computed_from_the_marks(tmp_path: Path) -> None:
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1]),
    )

    assert report.item_count == 1
    assert report.agreement["readability"].sample_size == 1
    assert report.agreement["readability"].exact_agreement == pytest.approx(1.0)
    # 決定的評価の観点も、教員が採点していれば比較対象になる。
    assert report.agreement["correctness"].sample_size == 1


@needs_c_compiler
def test_a_disagreement_shows_up_as_bias_and_a_miss(tmp_path: Path) -> None:
    """AI が段階 3、教員が 1。自動確定していれば見逃しとして数える。"""
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_3]),
    )

    readability = report.agreement["readability"]
    assert readability.exact_agreement == pytest.approx(0.0)
    # 教員 1 − AI 3 = −2。負なので AI が甘い。
    assert readability.mean_bias == pytest.approx(-2.0)
    assert report.observed_miss_rate == pytest.approx(1.0)
    assert report.verdict is Verdict.FAIL


@needs_c_compiler
def test_a_thin_sample_reports_not_measured_even_on_perfect_agreement() -> None:
    """1 件の完全一致でも合格にしない。ここが足場の要。"""
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(min_sample_size=30),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1]),
    )
    assert report.agreement["readability"].exact_agreement == pytest.approx(1.0)
    assert report.verdict is Verdict.NOT_MEASURED
    assert "1/30" in next(c for c in report.checks if "κ" in c.name).detail


@needs_c_compiler
def test_an_llm_failure_is_recorded_not_silently_dropped() -> None:
    """評価器が落ちた提出を、一致した扱いにしてはならない。"""
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([], repeat=False),  # 応答なし = LLM 障害
    )
    assert report.items[0].unscored == ("readability",)
    # 採点できなかった観点は標本に入らない。
    assert "readability" not in report.agreement
    assert report.verdict is Verdict.NOT_MEASURED


@needs_c_compiler
def test_a_stored_run_is_measured_rather_than_a_fresh_one(tmp_path: Path) -> None:
    """教員がレビューした採点を測る。引き直すと別の結果を測ることになる。

    LLM は同じ提出でも実行ごとに違う段階を返しうる。測定のたびに採点し直すと、
    見逃し率もレビュー行き率も「誰も見ていない採点」の数字になる。
    """
    shutil.copytree(EXAMPLE_GOLDEN, tmp_path / "golden")
    items = load_golden(tmp_path / "golden", "cs_intro_c")
    item = items[0]

    # 教員のレビュー時に AI が段階 3 と判定した、という状況を作る。
    registry = _registry([VERDICT_3])
    profile = load_profile(PROFILES / "cs_intro_c.yaml", registry)
    payload = item.source_path.read_bytes()
    task = sharif_judge.import_problem(
        item.task_dir,
        subject_profile="cs_intro_c",
        authored_by=UserId(new_id("usr")),
        readability_weight=0.3,
    )
    run = GradingPipeline(registry, profile).run(
        task, _submission_of(item, payload), lambda _: payload
    )
    runs_dir = tmp_path / "golden" / "cs_intro_c" / "example-task" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "s001.json").write_text(
        json.dumps(run.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    assert stored_run(item) is not None

    # 測定時に AI が段階 1 を返す設定にしても、保存済みの 3 が測られる。
    report = run_evaluation(
        items,
        gates=_gates(),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1]),
    )
    assert report.reused_runs == 1
    assert report.regraded_runs == 0
    # 教員 1 − AI 3 = −2。段階 1 を測っていたら 0 になっていた。
    assert report.agreement["readability"].mean_bias == pytest.approx(-2.0)


@needs_c_compiler
def test_regrade_forces_a_fresh_grading_and_says_so(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLE_GOLDEN, tmp_path / "golden")
    items = load_golden(tmp_path / "golden", "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1]),
        regrade=True,
    )
    assert report.regraded_runs == 1
    # 引き直したことをレポートに明記する。黙って測ると実績と誤読される。
    assert "採点し直しています" in render(report)


def _submission_of(item, payload: bytes):
    """テスト用の Submission を組む。"""
    submission_id = SubmissionId(new_id("sub"))
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        storage_key=f"file://{item.source_path}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    return Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        submitted_at=datetime(2026, 4, 1, tzinfo=UTC),
    )


@needs_c_compiler
def test_repeated_grading_measures_consistency() -> None:
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(max_score_stdev=0.05),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1] * 4),
        repeats=3,
    )
    # 同じ応答を返すスタブなので、ばらつきはゼロ。
    assert report.observed_score_stdev == pytest.approx(0.0)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_an_empty_golden_set_exits_not_measured_not_pass(tmp_path: Path, capsys) -> None:
    """CI が「測れていない」を「合格」と取り違えないための境界。"""
    code = main(
        [
            "--subject",
            "cs_intro_c",
            "--golden",
            str(tmp_path),
            "--gates",
            str(GATES_PATH),
            "--profiles",
            str(PROFILES),
        ]
    )
    assert code == EXIT_NOT_MEASURED
    assert "合格ではありません" in capsys.readouterr().err


@needs_c_compiler
def test_the_report_says_why_it_could_not_measure() -> None:
    items = load_golden(EXAMPLE_GOLDEN, "cs_intro_c")
    report = run_evaluation(
        items,
        gates=_gates(min_sample_size=30),
        subject_profile="cs_intro_c",
        profile_path=PROFILES / "cs_intro_c.yaml",
        registry=_registry([VERDICT_1]),
    )
    text = render(report)
    assert "判定不能" in text
    assert "測れていないことは合格ではない" in text
    assert "混同行列" in text
