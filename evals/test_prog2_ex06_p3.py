"""実データによるエンドツーエンド検証。

検証する仮説（ADR 0002 / 設計原則 P1）:

    「科目プロファイルの宣言だけで採点が動く」

このテストは採点エンジンに科目固有のコードが 1 行も無い状態で、
実際の授業課題（prog2-2025 ex06/p3）を取り込んで採点する。
エンジン（aijudge_grading）は code_test_runner を import していない
— entry point で発見し、YAML が名前で指名しているだけ。

fixtures/README.md の注意書きも参照のこと（課題資料は再配布不可）。
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    EvaluatorStatus,
    Routing,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core.ids import ArtifactId, CourseId, SubmissionId, TaskVersionId, TenantId, UserId
from aijudge_grading import (
    EvaluatorRegistry,
    GradingPipeline,
    default_registry,
    grading_completed_event,
    load_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "prog2-2025-ex06-p3"
PROFILE_PATH = REPO_ROOT / "subjects" / "cs_intro_c.yaml"

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
INSTRUCTOR = UserId(new_id("usr"))
COURSE = CourseId("crs_" + "0" * 32)
LEARNER = UserId(new_id("usr"))
TENANT = TenantId(new_id("ten"))

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)


# --------------------------------------------------------------------------
# 取り込み
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def task_version():
    return sharif_judge.import_problem(
        FIXTURE,
        course_id=COURSE,
        subject_profile="cs_intro_c",
        authored_by=INSTRUCTOR,
    )


def test_import_reads_the_real_assignment_format(task_version) -> None:
    title, tag = sharif_judge.parse_title(task_version.statement)
    assert title == "最大値・最小値・平均値"
    assert tag == "必須"
    assert len(task_version.test_cases) == 5
    assert task_version.reference_solution is not None
    assert "scanf" in task_version.reference_solution
    # 取り込んだ課題は既存運用のものなので承認済みとして入る。
    assert task_version.is_published


def test_imported_test_cases_keep_input_and_expected_output(task_version) -> None:
    case1 = task_version.test_cases[0]
    assert case1.evaluator_id == "code_test_runner"
    assert case1.payload["input"].split() == ["-1", "0", "1", "2"]
    assert case1.payload["expected"].strip() == "2 2 2.000"
    assert case1.hidden is True


def test_import_fails_loudly_on_a_broken_directory(tmp_path: Path) -> None:
    (tmp_path / "desc.md").write_text("本文だけで見出しがない\n", encoding="utf-8")
    with pytest.raises(sharif_judge.ImportError_, match="markdown heading"):
        sharif_judge.import_problem(
            tmp_path, course_id=COURSE, subject_profile="cs_intro_c", authored_by=INSTRUCTOR
        )


# --------------------------------------------------------------------------
# プロファイルと登録
# --------------------------------------------------------------------------


def test_the_evaluator_is_discovered_through_an_entry_point() -> None:
    """エンジンは評価器を import していない。entry point で見つけている。

    決定的評価器の**個数**は固定しない。評価器を足せることがこの設計の
    目的で、足すたびにこのテストが落ちるなら、テストが設計を邪魔している。
    """
    registry = default_registry()
    assert "code_test_runner" in registry
    deterministic = registry.ids_of_kind(EvaluatorKind.DETERMINISTIC)
    assert "code_test_runner" in deterministic
    # 伴走プロセスの評価器も同じ経路で見つかる（ADR 0008）。
    assert "network_test_runner" in deterministic
    assert "rubric_ai_judge" not in deterministic


def test_profile_validation_rejects_an_unknown_evaluator(tmp_path: Path) -> None:
    """存在しない評価器名はロード時に落とす（ADR 0002）。"""
    path = tmp_path / "broken.yaml"
    path.write_text("name: broken\ndeterministic: [does_not_exist]\n", encoding="utf-8")
    with pytest.raises(KeyError, match="does_not_exist"):
        load_profile(path, default_registry())


def test_profile_validation_rejects_a_miscategorised_evaluator(tmp_path: Path) -> None:
    """決定的評価器を ai_evaluators に書いたら落とす。"""
    path = tmp_path / "miscategorised.yaml"
    path.write_text("name: x\nai_evaluators: [code_test_runner]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="declares kind"):
        load_profile(path, default_registry())


# --------------------------------------------------------------------------
# 採点（エンドツーエンド）
# --------------------------------------------------------------------------


def _submission_of(source: str) -> tuple[Submission, dict[ArtifactId, bytes]]:
    submission_id = SubmissionId(new_id("sub"))
    payload = source.encode()
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        filename="maxmin.c",
        storage_key=f"memory://{submission_id}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=NOW,
    )
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=LEARNER,
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    return submission, {artifact.id: payload}


def _pipeline() -> GradingPipeline:
    registry = default_registry()
    return GradingPipeline(registry, load_profile(PROFILE_PATH, registry))


def _grade(source: str, task_version):
    submission, contents = _submission_of(source)
    pipeline = _pipeline()
    run = pipeline.run(task_version, submission, lambda artifact: contents[artifact.id])
    return run, submission


@needs_c_compiler
def test_the_reference_solution_scores_full_marks(task_version) -> None:
    run, _ = _grade(task_version.reference_solution, task_version)

    assert run.score_ratio == pytest.approx(1.0)
    assert run.confidence == pytest.approx(1.0)
    assert run.routing is Routing.AUTO
    score = run.criterion_scores[0]
    assert score.kind is EvaluatorKind.DETERMINISTIC
    assert score.conclusive is True
    assert score.level == 3
    assert "5 件すべて" in score.rationale


@needs_c_compiler
def test_a_comma_separated_answer_says_what_is_wrong(task_version) -> None:
    """**書式の食い違いは全ケースを同時に落とす。**

    課題文は「1つの半角空白文字で区切られた一行に出力する」と書いている。
    論理が正しくても区切りが違えば 0 件一致になり、根拠が「0 件が一致」
    だけだと、解けていない提出と見分けが付かない。
    """
    source = task_version.reference_solution.replace('"%d %d %.3f', '"%d,%d,%.3f')
    assert source != task_version.reference_solution
    run, _ = _grade(source, task_version)

    score = run.criterion_scores[0]
    assert run.score_ratio == pytest.approx(0.0)
    # 判定は変えない ── 書式指定も課題の一部。
    assert "区切り方" in score.rationale
    assert "課題文の出力形式" in score.rationale


@needs_c_compiler
def test_a_partially_correct_submission_gets_partial_credit(task_version) -> None:
    """n <= 0 の再入力を実装し忘れた提出。ケース 1 と 5 だけ落ちる。"""
    source = """
#include <stdio.h>
int main(void) {
    int n;
    scanf("%d", &n);
    int max = 0, min = 0, sum = 0;
    for (int i = 0; i < n; i++) {
        int a; scanf("%d", &a);
        if (i == 0 || a > max) max = a;
        if (i == 0 || a < min) min = a;
        sum += a;
    }
    printf("%d %d %.3f\\n", max, min, (double)sum / n);
    return 0;
}
"""
    run, _ = _grade(source, task_version)

    assert 0.0 < run.score_ratio < 1.0
    cases = run.evaluator_results[0].raw_output["cases"]
    failed = [case["name"] for case in cases if not case["passed"]]
    assert failed == ["case1", "case5"]
    # 決定的に確定した観点なので、確信度は下がらない。
    assert run.criterion_scores[0].conclusive is True


@needs_c_compiler
def test_a_compile_error_is_a_zero_not_an_evaluator_failure(task_version) -> None:
    """コンパイルエラーは 0 点で確定であって、採点の失敗ではない（§04 step 2）。"""
    run, _ = _grade("int main(void) { this is not C }\n", task_version)

    assert run.score_ratio == pytest.approx(0.0)
    assert run.evaluator_results[0].status is EvaluatorStatus.OK
    assert "コンパイルに失敗" in run.criterion_scores[0].rationale
    assert "compile_error" in run.evaluator_results[0].raw_output


@needs_c_compiler
def test_an_infinite_loop_is_caught_by_the_timeout(task_version) -> None:
    """CPU を回し続ける提出を、壁時計ではなく CPU 上限で早く打ち切る。"""
    registry = default_registry()
    # 科目プロファイルの実行上限は設定なので、テストでは短くして差し替えられる。
    profile = load_profile(PROFILE_PATH, registry).model_copy(update={"timeout_seconds": 1.0})
    submission, contents = _submission_of("int main(void) { for (;;) ; }\n")
    run = GradingPipeline(registry, profile).run(
        task_version, submission, lambda artifact: contents[artifact.id]
    )

    assert run.score_ratio == pytest.approx(0.0)
    cases = run.evaluator_results[0].raw_output["cases"]
    assert len(cases) == 5
    assert all(case["reason"] == "timeout" for case in cases), [c["reason"] for c in cases]


@needs_c_compiler
def test_grading_is_reproducible_for_identical_input(task_version) -> None:
    """同一入力の再採点で input_hash が一致する（設計原則 P8）。"""
    first, _ = _grade(task_version.reference_solution, task_version)
    second, _ = _grade(task_version.reference_solution, task_version)

    assert first.context.input_hash == second.context.input_hash
    assert first.score_ratio == second.score_ratio
    assert first.context.pipeline_version == second.context.pipeline_version
    # run そのものは別物。再採点は上書きしない。
    assert first.id != second.id


@needs_c_compiler
def test_the_run_converts_to_an_event_for_downstream_subsystems(task_version) -> None:
    run, submission = _grade(task_version.reference_solution, task_version)
    event = grading_completed_event(run, submission, tenant_id=TENANT)

    assert event.type == "grading.completed"
    assert event.score_ratio == run.score_ratio
    assert event.learner_id == LEARNER
    # S7 はこのイベントだけを見る。GradingRun の内部構造を知らない（P6）。


@needs_c_compiler
def test_grading_completes_with_ai_evaluators_disabled(task_version) -> None:
    """AI 評価器を外しても採点は完結する（設計原則 P2）。

    S6（LLM Gateway）を止めた状態の劣化動作にあたる。この課題は
    テスト実行だけで採点できる観点しか持たないので、点は満点のまま。
    """
    registry = EvaluatorRegistry().load_installed()
    profile = load_profile(PROFILE_PATH, registry).model_copy(
        update={"ai_evaluators": (), "evaluator_options": {}}
    )

    submission, contents = _submission_of(task_version.reference_solution)
    run = GradingPipeline(registry, profile).run(
        task_version, submission, lambda artifact: contents[artifact.id]
    )
    assert run.score_ratio == pytest.approx(1.0)


def test_the_case_timeout_is_separate_from_the_evaluator_budget() -> None:
    """テストケースの実行上限が、LLM の予算に引きずられないこと。

    科目プロファイルの `timeout_seconds` は評価器 1 回の呼び出しの予算で、
    AI 評価器では LLM の応答待ちを含む（120 秒）。同じ値を実行上限に使うと、
    暴走コードに 120 秒 × ケース数の猶予を与える。

    実測（2026-08-28）: 分けていなかったため、無限ループの提出 1 件で
    ワーカーが 10 分占有された。締切前に数件あれば待ち行列が止まる。
    """
    from pathlib import Path

    from aijudge_eval_code_test_runner import (
        DEFAULT_CASE_TIMEOUT_SECONDS,
        OPTION_CASE_TIMEOUT,
    )
    from aijudge_grading import load_profile

    profile = load_profile(Path(__file__).resolve().parents[1] / "subjects" / "cs_intro_c.yaml")
    options = profile.evaluator_options.get("code_test_runner", {})
    case_timeout = float(options.get(OPTION_CASE_TIMEOUT, DEFAULT_CASE_TIMEOUT_SECONDS))

    assert case_timeout < profile.timeout_seconds, (
        "テストケースの実行上限が評価器の予算と同じになっている"
    )
    # 5 ケースの課題で、暴走した提出が占有する時間の上限。
    assert case_timeout * 5 < 30.0, (
        f"暴走した提出が {case_timeout * 5:.0f} 秒占有する。"
        "§9.1 の「結果表示まで p95 < 30 秒」を満たせない"
    )


def test_an_absurd_case_timeout_option_falls_back_to_the_default() -> None:
    """設定ミスで採点が止まるより、既定で動いた方がよい。"""
    from aijudge_eval_code_test_runner import (
        DEFAULT_CASE_TIMEOUT_SECONDS,
        OPTION_CASE_TIMEOUT,
        _seconds_option,
    )

    bad_options = (
        {},
        {OPTION_CASE_TIMEOUT: "abc"},
        {OPTION_CASE_TIMEOUT: 0},
        {OPTION_CASE_TIMEOUT: -5},
    )
    for bad in bad_options:
        assert (
            _seconds_option(bad, OPTION_CASE_TIMEOUT, DEFAULT_CASE_TIMEOUT_SECONDS)
            == DEFAULT_CASE_TIMEOUT_SECONDS
        )


# --------------------------------------------------------------------------
# 2 つめの言語（Phase 2）
# --------------------------------------------------------------------------


def test_the_runner_supports_more_than_one_language() -> None:
    """言語を足す作業が評価器に閉じていること（ADR 0002）。"""
    from aijudge_eval_code_test_runner import LANGUAGES, resolve_language

    assert {"c", "python"} <= set(LANGUAGES)
    assert resolve_language({}).label == "C", "既定は C"
    assert resolve_language({"language": "python"}).label == "Python"
    assert resolve_language({"language": "PYTHON"}).label == "Python", "大文字小文字を問わない"


def test_a_script_language_has_no_compile_step() -> None:
    """「コンパイルエラー」という結果が存在しない言語がある。"""
    from aijudge_eval_code_test_runner import resolve_language

    assert resolve_language({"language": "c"}).compile_argv is not None
    assert resolve_language({"language": "python"}).compile_argv is None


def test_an_unknown_language_stops_the_grading() -> None:
    """既定に落とさない。落とすと言語違いが「全員 0 点」として現れる。"""
    from aijudge_eval_code_test_runner import UnknownLanguage, resolve_language

    with pytest.raises(UnknownLanguage, match="not supported"):
        resolve_language({"language": "rust"})


def test_python_is_run_in_isolated_mode() -> None:
    """`-I` で環境変数と site-packages を無視する。

    無視しないと、提出の挙動が採点機の状態に依存する（PYTHONPATH に
    何が入っているかで結果が変わる）。
    """
    from aijudge_eval_code_test_runner import resolve_language

    assert "-I" in resolve_language({"language": "python"}).run_argv


def test_both_subject_profiles_declare_their_language() -> None:
    """科目の違いが YAML に現れていること。"""
    from pathlib import Path

    from aijudge_grading import EvaluatorRegistry, load_profile

    registry = EvaluatorRegistry().load_installed()
    root = Path(__file__).resolve().parents[1] / "subjects"
    languages = {}
    for path in sorted(root.glob("*.yaml")):
        profile = load_profile(path, registry)
        options = profile.evaluator_options.get("code_test_runner", {})
        languages[profile.name] = options.get("language")

    assert languages.get("cs_intro_c") == "c"
    assert languages.get("net_python") == "python"


def test_a_common_error_is_reported_instead_of_case_names() -> None:
    """全件が同じ理由で落ちたなら、その理由を書く。

    スクリプト言語には コンパイル段階が無いので、構文エラーが「全件の実行時
    失敗」として現れる。ケース名を並べるだけでは「出力が違う」としか伝わらない。
    """
    from aijudge_eval_code_test_runner import CodeTestRunner

    runner = CodeTestRunner()
    failed = [
        {"name": f"case{i}", "stderr": "Traceback...\nSyntaxError: '(' was never closed"}
        for i in range(1, 6)
    ]
    rationale = runner._rationale(0, 5, failed)
    assert "SyntaxError: '(' was never closed" in rationale
    assert "case1" not in rationale


def test_differing_errors_fall_back_to_case_names() -> None:
    """理由が違うなら、まとめて 1 つのエラーとして語らない。"""
    from aijudge_eval_code_test_runner import CodeTestRunner

    runner = CodeTestRunner()
    failed = [
        {"name": "case1", "stderr": "ValueError: a"},
        {"name": "case2", "stderr": "KeyError: b"},
    ]
    rationale = runner._rationale(0, 2, failed)
    assert "case1" in rationale


def test_a_shared_signal_is_reported(monkeypatch) -> None:
    """C の segfault も「全件同じ理由」に当たる。"""
    from aijudge_eval_code_test_runner import CodeTestRunner

    runner = CodeTestRunner()
    failed = [{"name": f"case{i}", "signal": "SIGSEGV"} for i in range(3)]
    assert "SIGSEGV" in runner._rationale(0, 3, failed)
