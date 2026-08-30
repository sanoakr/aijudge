"""フィードバックの規則を固定する。

固定したいのは 2 つ。

漏らさない  確定していない AI の判定を材料にしない。それを材料にすると、
            学習者に見せないことにした判断が文章の形で漏れる（設計原則 P5）。
黙らない    S6 が停止しても何かを返す。無言だと、学習者にはシステムが
            壊れたのか提出に問題が無いのか区別できない（設計原則 P2）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingContext,
    GradingRun,
    Provenance,
    Routing,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
)
from aijudge_core.ids import (
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    GradingRunId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_feedback import FeedbackGenerator, releasable_scores, summarize_findings
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_llm_gateway.provider import Provider
from aijudge_llm_gateway.types import LlmError, LlmRequest, LlmResponse, ProviderCapabilities

NOW = datetime(2026, 8, 28, tzinfo=UTC)
CORRECTNESS = CriterionId("crt_" + "1" * 32)
READABILITY = CriterionId("crt_" + "2" * 32)

AI_RATIONALE = "AIRATIONALEMARKER 変数名から役割が読み取れません"
DETERMINISTIC_RATIONALE = "5 件中 3 件のテストが失敗しました（n <= 0 の再入力）"


def _criterion(criterion_id: CriterionId, code: str, title: str, weight: float):
    return RubricCriterion(
        id=criterion_id,
        code=code,
        title=title,
        description=f"{title}の観点",
        weight=weight,
        levels=(
            RubricLevel(level=0, label="不可", descriptor="満たさない", score_ratio=0.0),
            RubricLevel(level=3, label="良", descriptor="満たす", score_ratio=1.0),
        ),
    )


def _task() -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "3" * 32),
        task_id=TaskId("tsk_" + "4" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="標準入力から n を読み、最大値・最小値・平均値を出力せよ。",
        criteria=(
            _criterion(CORRECTNESS, "correctness", "正しさ", 0.7),
            _criterion(READABILITY, "readability", "読みやすさ", 0.3),
        ),
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "5" * 32)),
        created_at=NOW,
    )


def _score(criterion_id: CriterionId, kind: EvaluatorKind, rationale: str, weight: float):
    return CriterionScore(
        id=CriterionScoreId("cs_" + criterion_id[-4:] + "0" * 28),
        criterion_id=criterion_id,
        evaluator_result_id=EvaluatorResultId("evr_" + "6" * 32),
        kind=kind,
        level=1,
        score_ratio=0.4,
        weight=weight,
        confidence=1.0,
        conclusive=kind is EvaluatorKind.DETERMINISTIC,
        evidence=(),
        rationale=rationale,
    )


def _run(*, with_ai: bool = True, unscored: tuple = ()) -> GradingRun:
    scores = [_score(CORRECTNESS, EvaluatorKind.DETERMINISTIC, DETERMINISTIC_RATIONALE, 0.7)]
    if with_ai:
        # AI のスコアは evidence が必須（P4）。
        from aijudge_core.ids import ArtifactId
        from aijudge_core.spans import Evidence, LineSpan

        scores.append(
            _score(READABILITY, EvaluatorKind.AI, AI_RATIONALE, 0.3).model_copy(
                update={
                    "evidence": (
                        Evidence(
                            artifact_id=ArtifactId("art_" + "9" * 32),
                            artifact_content_hash="sha256:abc",
                            span=LineSpan(start_line=5, end_line=5),
                            quote="int b = 0;",
                        ),
                    )
                }
            )
        )
    else:
        scores[0] = scores[0].model_copy(update={"weight": 1.0})

    # 未採点の観点にはスコアが無い。両方に置くと、その観点の重みが二重に
    # 効いた記録になる（コアの検証が落とす）。
    scores = [score for score in scores if score.criterion_id not in unscored]

    return GradingRun(
        id=GradingRunId("grn_" + "7" * 32),
        submission_id=SubmissionId("sub_" + "8" * 32),
        context=GradingContext(
            task_version_id=TaskVersionId("tsv_" + "3" * 32),
            subject_profile="cs_intro_c",
            rubric_version="v1",
            input_hash="sha256:abc",
            pipeline_version="0.1.0",
        ),
        criterion_scores=tuple(scores),
        score_ratio=0.4,
        confidence=1.0,
        routing=Routing.REVIEW_REQUIRED if unscored else Routing.AUTO,
        unscored_criteria=unscored,
        created_at=NOW,
    )


SOURCE = "#include <stdio.h>\nint main(void){int b = 0;return 0;}\n"


# --------------------------------------------------------------------------
# 材料の制約 — 設計原則 P5
# --------------------------------------------------------------------------


def test_only_deterministic_results_are_material() -> None:
    """AI の判定は確定前の値なので材料にしない。"""
    scores = releasable_scores(_run())
    assert [score.criterion_id for score in scores] == [CORRECTNESS]


def test_the_findings_do_not_mention_the_ai_verdict() -> None:
    findings = "\n".join(summarize_findings(_run(), _task()))
    assert DETERMINISTIC_RATIONALE in findings
    assert AI_RATIONALE not in findings, "AI の判定が材料に混ざっている"


def test_the_prompt_does_not_carry_the_ai_verdict() -> None:
    """プロンプトに載せた時点で、出力に漏れる可能性が生まれる。"""
    provider = ScriptedProvider(['{"message": "n <= 0 の場合を確かめてください。"}'])
    FeedbackGenerator(LlmGateway(provider), model="stub").generate(_run(), _task(), SOURCE)

    assert provider.calls
    prompt = "\n".join(message.content for message in provider.calls[0].messages)
    assert DETERMINISTIC_RATIONALE in prompt
    assert AI_RATIONALE not in prompt


def test_unscored_criteria_are_mentioned_without_the_verdict() -> None:
    """暫定であることを隠さない。"""
    findings = summarize_findings(_run(unscored=(READABILITY,)), _task())
    assert any("担当教員が確認します" in line for line in findings)


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def test_the_message_comes_from_the_model() -> None:
    provider = ScriptedProvider(
        ['{"message": "n <= 0 のときに再入力を求める処理を足してください。"}']
    )
    result = FeedbackGenerator(LlmGateway(provider), model="stub").generate(_run(), _task(), SOURCE)
    assert result is not None
    assert not result.fallback
    assert "再入力" in result.message
    assert result.model_id == "stub"
    assert result.prompt_id == "next_step_feedback@1"


def test_the_source_is_clipped(monkeypatch) -> None:
    """長い提出でコンテキストを食い潰し、肝心のテスト結果を押し出さない。"""
    provider = ScriptedProvider(['{"message": "確認してください。"}'])
    generator = FeedbackGenerator(LlmGateway(provider), model="stub", max_source_chars=50)
    generator.generate(_run(), _task(), "x" * 5000)

    prompt = "\n".join(m.content for m in provider.calls[0].messages)
    assert "以下省略" in prompt
    assert len(prompt) < 3000


def test_a_long_message_is_clipped() -> None:
    provider = ScriptedProvider(['{"message": "' + "あ" * 5000 + '"}'])
    result = FeedbackGenerator(LlmGateway(provider), model="stub").generate(_run(), _task(), SOURCE)
    assert result is not None
    assert len(result.message) <= 1220


def test_nothing_to_say_returns_nothing() -> None:
    """材料が無ければ黙る。空の助言を返すより無い方がよい。"""
    run = _run(with_ai=False)
    run = run.model_copy(update={"criterion_scores": ()})
    # criterion_scores は min_length=1 なので、AI のみの run で代用する。
    ai_only = _run()
    ai_only = ai_only.model_copy(
        update={
            "criterion_scores": (ai_only.criterion_scores[1].model_copy(update={"weight": 1.0}),)
        }
    )
    assert FeedbackGenerator().generate(ai_only, _task(), SOURCE) is None


# --------------------------------------------------------------------------
# 劣化動作 — 設計原則 P2
# --------------------------------------------------------------------------


def test_without_a_gateway_the_summary_is_returned() -> None:
    """S6 を導入していない運用でも何かを返す。"""
    result = FeedbackGenerator().generate(_run(), _task(), SOURCE)
    assert result is not None
    assert result.fallback
    assert DETERMINISTIC_RATIONALE in result.message
    assert AI_RATIONALE not in result.message


def test_a_failing_provider_falls_back_instead_of_going_silent() -> None:
    """無言だと、壊れたのか提出に問題が無いのか区別できない。"""

    class Broken(Provider):
        name = "broken"
        capabilities = ProviderCapabilities(local=True)

        def complete(self, request: LlmRequest) -> LlmResponse:
            raise LlmError("the model host is down")

    result = FeedbackGenerator(LlmGateway(Broken()), model="stub").generate(_run(), _task(), SOURCE)
    assert result is not None
    assert result.fallback
    assert DETERMINISTIC_RATIONALE in result.message


def test_an_empty_message_falls_back() -> None:
    provider = ScriptedProvider(['{"message": "   "}'] * 3)
    result = FeedbackGenerator(LlmGateway(provider), model="stub").generate(_run(), _task(), SOURCE)
    assert result is not None
    assert result.fallback


def test_learner_data_cannot_go_to_a_remote_provider() -> None:
    """提出コードを学外へ送らない（設計原則 P7）。

    ゲートウェイが拒否し、フィードバックは要約に落ちる。
    """
    provider = ScriptedProvider(['{"message": "x"}'], local=False)
    result = FeedbackGenerator(LlmGateway(provider), model="stub").generate(_run(), _task(), SOURCE)
    assert result is not None
    assert result.fallback, "学外プロバイダに提出コードが送られた"
    assert provider.calls == []


def test_the_prompt_version_is_pinned() -> None:
    """文面を直したら版を上げる（P8）。過去のフィードバックの出所が辿れる。"""
    from aijudge_feedback import FEEDBACK_PROMPT

    assert FEEDBACK_PROMPT.id == "next_step_feedback@1"


def test_the_system_prompt_forbids_giving_the_answer() -> None:
    """解答そのものを書かせない。学習者が自分で直せる手がかりに留める。"""
    from aijudge_feedback import FEEDBACK_PROMPT

    assert FEEDBACK_PROMPT.system is not None
    assert "解答そのもの" in FEEDBACK_PROMPT.system
    assert "点数や合否に言及しない" in FEEDBACK_PROMPT.system


@pytest.mark.parametrize("weight", [0.7, 1.0])
def test_the_finding_names_the_criterion(weight: float) -> None:
    findings = summarize_findings(_run(), _task())
    assert any("正しさ" in line for line in findings)
