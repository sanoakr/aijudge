"""確定済みの観点は、教員が書いた定義の言葉だけでモデルに渡す（プロンプト版 5）。

決定的評価器の根拠文には、学習者のプログラムが出した文字列が入りうる。
「すでに確定していること」の節は提出物の境界の外にあるので、そこには課題の
定義（観点の題名・段階の名前と記述）と点の割合だけを置く。
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_source_selection import _verdict_json

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    CriterionScore,
    EvaluatorKind,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    CriterionScoreId,
    EvaluatorResultId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_eval_rubric_ai_judge import RubricAiJudge, describe_prior
from aijudge_grading.protocol import EvaluationRequest
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
CORRECT = CriterionId("crt_" + "1" * 32)
STYLE = CriterionId("crt_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUB = SubmissionId("sub_" + "4" * 32)
# 学習者のプログラムが出した、と見立てた文字列。プロンプトのどこにも現れてはいけない。
FROM_THE_PROGRAM = "PROGRAM-OUTPUT-MARKER"


def _criterion(cid: CriterionId, code: str, title: str) -> RubricCriterion:
    return RubricCriterion(
        id=cid,
        code=code,
        title=title,
        description=f"{title}の観点",
        weight=0.5,
        levels=(
            RubricLevel(
                level=0, label="未達", descriptor="ほとんど正しく動作しない", score_ratio=0.0
            ),
            RubricLevel(
                level=1, label="達成", descriptor="すべてのケースで正しい", score_ratio=1.0
            ),
        ),
    )


def _request(prior_rationale: str, *, prior_criterion: CriterionId = CORRECT) -> EvaluationRequest:
    correct = _criterion(CORRECT, "correctness", "出力の正しさ")
    style = _criterion(STYLE, "readability", "読みやすさ")
    payload = b"int main(void) { return 1; }\n"
    return EvaluationRequest(
        task_version=TaskVersion(
            id=TaskVersionId("tsv_" + "5" * 32),
            task_id=TaskId("tsk_" + "6" * 32),
            version=1,
            subject_profile="cs_lang_c_intro",
            statement="## 問題 ##",
            criteria=(correct, style),
            max_score=10.0,
            provenance=Provenance(authored_by=UserId("usr_" + "7" * 32)),
            created_at=NOW,
        ),
        submission=Submission(
            id=SUB,
            task_version_id=TaskVersionId("tsv_" + "5" * 32),
            learner_id=UserId("usr_" + "8" * 32),
            state=SubmissionState.SUBMITTED,
            submitted_at=NOW,
            created_at=NOW,
            artifacts=(
                Artifact(
                    id=ARTIFACT,
                    submission_id=SUB,
                    role=ArtifactRole.ORIGINAL,
                    kind=ArtifactKind.CODE,
                    storage_key="k",
                    content_hash="sha256:x",
                    byte_size=len(payload),
                    filename="main.c",
                    created_at=NOW,
                ),
            ),
        ),
        artifact_contents={ARTIFACT: payload},
        criterion=style,
        prior_results=(
            CriterionScore(
                id=CriterionScoreId("cs_" + "9" * 32),
                criterion_id=prior_criterion,
                evaluator_result_id=EvaluatorResultId("evr_" + "a" * 32),
                kind=EvaluatorKind.DETERMINISTIC,
                level=0,
                score_ratio=0.0,
                weight=0.5,
                confidence=1.0,
                conclusive=True,
                rationale=prior_rationale,
            ),
        ),
    )


def _prompt(request: EvaluationRequest) -> str:
    provider = ScriptedProvider([_verdict_json(1)])
    RubricAiJudge(LlmGateway(provider), model="stub", samples=1).evaluate(request)
    return "\n".join(message.content for message in provider.calls[0].messages)


def test_the_evaluators_own_words_do_not_reach_the_model() -> None:
    rationale = (
        "テストケース 3 件中 0 件が一致しました。"
        + f"すべて同じエラーで停止しています: {FROM_THE_PROGRAM}"
    )
    assert FROM_THE_PROGRAM not in _prompt(_request(rationale))


def test_the_settled_criterion_is_described_by_its_definition() -> None:
    text = describe_prior(_request("何でもよい"))
    assert "出力の正しさ" in text
    assert "0%" in text
    assert "未達" in text and "ほとんど正しく動作しない" in text
    assert "何でもよい" not in text


def test_a_score_for_a_criterion_the_task_lacks_is_left_out() -> None:
    """課題の定義から引けない観点は、題名も段階も教員の言葉で言えないので出さない。"""
    stray = CriterionId("crt_" + "f" * 32)
    text = describe_prior(_request(FROM_THE_PROGRAM, prior_criterion=stray))
    assert FROM_THE_PROGRAM not in text
    assert stray not in text
