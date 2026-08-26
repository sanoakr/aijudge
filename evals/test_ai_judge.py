"""AI 評価器の規則を、LLM を呼ばずに検証する。

実 LLM を叩く検証は test_llm_live.py（AIJUDGE_LIVE_LLM=1 のときだけ走る）。
ここで固定するのは、モデルが何を返そうと崩れてはいけない性質。
"""

from __future__ import annotations

import hashlib
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
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_eval_rubric_ai_judge import RubricAiJudge, number_lines
from aijudge_grading import EvaluatorRegistry, GradingPipeline, load_profile
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "prog2-2025-ex06-p3"
PROFILE_PATH = REPO_ROOT / "subjects" / "cs_intro_c.yaml"

NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
INSTRUCTOR = UserId(new_id("usr"))


@pytest.fixture(scope="module")
def task_version():
    """正しさ 0.7 / 読みやすさ 0.3 の 2 観点にして取り込む。"""
    return sharif_judge.import_problem(
        FIXTURE,
        subject_profile="cs_intro_c",
        authored_by=INSTRUCTOR,
        readability_weight=0.3,
    )


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
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    return submission, {artifact.id: payload}


def _judge(responses: list[str]) -> tuple[RubricAiJudge, ScriptedProvider]:
    provider = ScriptedProvider(responses)
    return RubricAiJudge(LlmGateway(provider), model="stub"), provider


def _pipeline(judge: RubricAiJudge, samples: int = 1) -> GradingPipeline:
    """テスト用に、AI 評価器だけ差し替えたレジストリを組む。

    決定的評価器は entry point から本物を拾う。サンプル数は科目プロファイルの
    設定が評価器の既定より優先されるので（ADR 0002: 設定面はプロファイル）、
    プロファイル側を差し替える。
    """
    registry = EvaluatorRegistry().load_installed()
    registry.replace(judge)
    profile = load_profile(PROFILE_PATH, registry).model_copy(
        update={"evaluator_options": {judge.evaluator_id: {"samples": samples}}}
    )
    return GradingPipeline(registry, profile)


VALID = (
    '{"level": 2, "evidence": [{"start_line": 4, "end_line": 7, '
    '"quote": "do { scanf }"}], "rationale": "変数名は概ね説明的です。"}'
)


# --------------------------------------------------------------------------
# 観点の分割（設計方針 §04 step 3）
# --------------------------------------------------------------------------


def test_the_rubric_now_has_a_criterion_tests_cannot_measure(task_version) -> None:
    codes = [criterion.code for criterion in task_version.criteria]
    assert codes == ["correctness", "readability"]
    weights = {c.code: c.weight for c in task_version.criteria}
    assert weights == {"correctness": pytest.approx(0.7), "readability": pytest.approx(0.3)}
    assert task_version.criteria[1].evaluator_id == "rubric_ai_judge"


def test_the_judge_is_called_once_per_criterion_not_once_per_submission(task_version) -> None:
    judge, provider = _judge([VALID])
    _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))
    # 観点は 2 つあるが、正しさは決定的評価が確定させたので AI は 1 回だけ。
    assert len(provider.calls) == 1
    prompt = provider.calls[0].messages[-1].content
    assert "# 今回評価する観点: 変数名と構造の分かりやすさ" in prompt
    assert "# 今回評価する観点: 出力の正しさ" not in prompt


def _wire(source: str):
    submission, contents = _submission_of(source)
    return submission, (lambda artifact: contents[artifact.id])


def test_deterministic_results_are_given_to_the_model_as_context(task_version) -> None:
    """これを渡すのが精度の鍵（§04 step 3）。"""
    judge, provider = _judge([VALID])
    _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    prompt = provider.calls[0].messages[-1].content
    assert "すでに確定していること" in prompt
    assert "[確定] 出力の正しさ: 100%" in prompt
    assert "5 件すべて" in prompt


def test_the_code_is_numbered_so_the_model_can_cite_lines() -> None:
    numbered = number_lines("int main(void) {\n    return 0;\n}")
    assert numbered.splitlines()[0] == "1 | int main(void) {"
    assert numbered.splitlines()[2] == "3 | }"


# --------------------------------------------------------------------------
# P3: 決定的評価が確定させた観点は AI に問い合わせない
# --------------------------------------------------------------------------


def test_a_settled_criterion_is_never_sent_to_the_llm(task_version) -> None:
    judge, provider = _judge([VALID])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    correctness = next(s for s in run.criterion_scores if s.conclusive)
    assert correctness.kind is EvaluatorKind.DETERMINISTIC
    # AI 呼び出しは 1 回きり。確定済み観点の分は費用も掛からない。
    assert len(provider.calls) == 1


# --------------------------------------------------------------------------
# P4: 根拠のないスコアは採用しない
# --------------------------------------------------------------------------


def test_an_empty_evidence_list_is_sent_back_for_repair(task_version) -> None:
    """根拠なしの判定は受け取らず、モデルに直させる（設計原則 P4）。

    実測で、モデルは根拠を rationale の文中には書きながら evidence を
    空で返すことがある。`evidence` を必須にしてあるので、これはスキーマ
    違反として再試行に回る。
    """
    empty = '{"level": 3, "evidence": [], "rationale": "よい"}'
    judge, provider = _judge([empty, empty, VALID])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    assert len(provider.calls) == 3
    assert "エラー" in provider.calls[-1].messages[-1].content
    ai_score = next(s for s in run.criterion_scores if s.kind is EvaluatorKind.AI)
    assert ai_score.evidence


def test_a_model_that_never_cites_evidence_yields_no_score(task_version) -> None:
    """直らなければ点を付けない。根拠のない点数は出さない。"""
    empty = '{"level": 3, "evidence": [], "rationale": "よい"}'
    judge, _ = _judge([empty, empty, empty])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    ai_result = next(r for r in run.evaluator_results if r.kind is EvaluatorKind.AI)
    assert ai_result.status is EvaluatorStatus.FAILED
    # AI の点は入らないが、決定的評価の点は残り採点は完結する。
    assert [s.kind for s in run.criterion_scores] == [EvaluatorKind.DETERMINISTIC]
    assert run.is_provisional


def test_fabricated_line_numbers_are_discarded(task_version) -> None:
    """存在しない行を指す根拠は捨てる。捏造を UI に出さないため。"""
    judge, _ = _judge(
        [
            '{"level": 2, "evidence": [{"start_line": 9999, "end_line": 10000, '
            '"quote": "?"}], "rationale": "x"}'
        ]
    )
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))
    ai_result = next(r for r in run.evaluator_results if r.kind is EvaluatorKind.AI)
    assert ai_result.status is EvaluatorStatus.FAILED


def test_evidence_beyond_the_last_line_is_clamped(task_version) -> None:
    """始点は実在し終点だけ行き過ぎている場合は、捨てずに末尾へ寄せる。"""
    judge, _ = _judge(
        [
            '{"level": 2, "evidence": [{"start_line": 3, "end_line": 9999, '
            '"quote": "?"}], "rationale": "x"}'
        ]
    )
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))
    ai_score = next(s for s in run.criterion_scores if s.kind is EvaluatorKind.AI)
    span = ai_score.evidence[0].span
    line_count = len(task_version.reference_solution.split("\n"))
    assert span.start_line == 3
    assert span.end_line == line_count


def test_evidence_points_at_real_lines_of_the_submission(task_version) -> None:
    judge, _ = _judge([VALID])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    ai_score = next(s for s in run.criterion_scores if s.kind is EvaluatorKind.AI)
    evidence = ai_score.evidence[0]
    assert evidence.span.kind == "line"
    assert (evidence.span.start_line, evidence.span.end_line) == (4, 7)
    # 提出物の現在の内容に対して有効な根拠であること。
    artifact = run.evaluator_results and evidence.artifact_id
    assert artifact is not None


# --------------------------------------------------------------------------
# 集約・振り分け・出所
# --------------------------------------------------------------------------


def test_the_ai_score_is_weighted_into_the_total(task_version) -> None:
    judge, _ = _judge([VALID])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    # 正しさ 1.0 × 0.7 ＋ 読みやすさ 0.67 × 0.3
    assert run.score_ratio == pytest.approx(0.7 + 0.67 * 0.3, abs=1e-6)


def test_a_split_ai_vote_sends_the_submission_to_human_review(task_version) -> None:
    """確信度が閾値を割ったら人間が見る（設計原則 P5）。"""
    provider = ScriptedProvider(
        [
            '{"level": 1, "evidence": [{"start_line": 1, "end_line": 2, "quote": "a"}], '
            '"rationale": "a"}',
            '{"level": 3, "evidence": [{"start_line": 1, "end_line": 2, "quote": "b"}], '
            '"rationale": "b"}',
            '{"level": 2, "evidence": [{"start_line": 1, "end_line": 2, "quote": "c"}], '
            '"rationale": "c"}',
        ]
    )
    judge = RubricAiJudge(LlmGateway(provider), model="stub")
    run = _pipeline(judge, samples=3).run(task_version, *_wire(task_version.reference_solution))

    ai_score = next(s for s in run.criterion_scores if s.kind is EvaluatorKind.AI)
    assert ai_score.confidence == pytest.approx(1 / 3)
    assert run.confidence == pytest.approx(1 / 3)
    assert run.routing is Routing.REVIEW_REQUIRED


def test_the_run_records_which_model_and_prompt_produced_it(task_version) -> None:
    """設計原則 P8。これが無いと後から採点を再現できない。"""
    judge, _ = _judge([VALID])
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    assert run.context.model_ids == {"rubric_ai_judge": "stub"}
    assert run.context.prompt_versions == {"rubric_ai_judge": "rubric_criterion_judge_ja@1"}


def test_grading_survives_the_llm_being_unavailable(task_version) -> None:
    """S6 が落ちても採点は完結する（設計原則 P2）。"""
    judge, _ = _judge([])  # 応答が尽きた = プロバイダ障害
    run = _pipeline(judge).run(task_version, *_wire(task_version.reference_solution))

    ai_result = next(r for r in run.evaluator_results if r.kind is EvaluatorKind.AI)
    assert ai_result.status is EvaluatorStatus.FAILED
    assert [s.kind for s in run.criterion_scores] == [EvaluatorKind.DETERMINISTIC]

    # 採点できた観点だけで暫定の点を出す（0 点にも満点にもしない）。
    assert run.score_ratio == pytest.approx(1.0)
    assert run.is_provisional
    readability = next(c.id for c in task_version.criteria if c.code == "readability")
    assert run.unscored_criteria == (readability,)
    # 誰も見ていない観点がある採点を自動確定させない（設計原則 P5）。
    assert run.routing is Routing.REVIEW_REQUIRED
