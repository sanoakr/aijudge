"""`rubric_ai_judge` の申告・段階の寄せ方・送るスキーマを固定する（#690）。

- **提出物は `DataClass.PERSONAL` で送る。** ゲートウェイの P7 検査は申告しか
  見ないので、ここを `NON_PERSONAL` に取り違えると、学外のプロバイダが
  設定されたとき解答がそのまま学外へ出る。既定の `ScriptedProvider` は
  `local=True` なので、学外を模さないと取り違えが見えない。
- **観点に無い段階は最も近い段階に寄せる**（`_clamp_level`）。`Verdict.level`
  には上限が無く、`level: 99` もスキーマ検証を通ってここへ来る。段階 0・3
  だけの観点で 1 が 0 に寄るか 3 に寄るかは合否を分ける。
- **ollama に送るスキーマ**は `$defs` / `$ref` / `maxLength` を落としても
  欄の名前と required を保つ（実スキーマで確かめる）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorStatus,
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
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_eval_rubric_ai_judge import EvidenceSpan, RubricAiJudge, Verdict
from aijudge_grading import EvaluationRequest
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_llm_gateway.provider import simplify_schema

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
ARTIFACT = ArtifactId("art_" + "a" * 32)
VERSION = TaskVersionId("tsv_" + "b" * 32)
SUBMISSION = SubmissionId("sub_" + "c" * 32)
REPORT = "1. 目的\n本実験の目的は性能の評価である。\n"


def _criterion() -> RubricCriterion:
    """段階 0 と 3 だけの観点。間の段階を返されたときの寄せ先が合否を分ける。"""
    return RubricCriterion(
        id=CriterionId("crt_" + "d" * 32),
        code="discussion",
        title="考察",
        description="結果から言えることと言えないことを区別しているか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="無い", score_ratio=0.0),
            RubricLevel(level=3, label="達成", descriptor="ある", score_ratio=1.0),
        ),
    )


def _request() -> EvaluationRequest:
    payload = REPORT.encode()
    criterion = _criterion()
    artifact = Artifact(
        id=ARTIFACT,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.MARKDOWN,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(payload),
        filename="report.md",
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=TaskVersion(
            id=VERSION,
            task_id=TaskId("tsk_" + "e" * 32),
            version=1,
            subject_profile="report_ja",
            statement="## レポート ##",
            criteria=(criterion,),
            max_score=100.0,
            provenance=Provenance(authored_by=UserId("usr_" + "f" * 32)),
            created_at=NOW,
        ),
        submission=Submission(
            id=SUBMISSION,
            task_version_id=VERSION,
            learner_id=UserId("usr_" + "0" * 32),
            state=SubmissionState.SUBMITTED,
            artifacts=(artifact,),
            submitted_at=NOW,
            created_at=NOW,
        ),
        artifact_contents={ARTIFACT: payload},
        criterion=criterion,
    )


def _verdict_json(level: int) -> str:
    return Verdict(
        observation="考察の節で測定条件の限界を述べている。",
        level=level,
        evidence=[EvidenceSpan(start_line=1, end_line=1, quote="1. 目的")],
        rationale="1 つ上の段階は無い。",
    ).model_dump_json()


def _judge(*levels: int, local: bool = True) -> tuple[RubricAiJudge, ScriptedProvider]:
    provider = ScriptedProvider([_verdict_json(level) for level in levels], local=local)
    return RubricAiJudge(LlmGateway(provider), model="stub", samples=len(levels)), provider


# --------------------------------------------------------------------------
# 機微度の申告（P7）
# --------------------------------------------------------------------------


def test_the_answer_is_never_sent_to_a_remote_provider() -> None:
    """学外のプロバイダには 1 度も呼び出しが届かず、評価は FAILED に落ちる。"""
    judge, provider = _judge(3, local=False)
    outcome = judge.evaluate(_request())

    assert provider.calls == [], "学習者の解答が学外プロバイダに送られた"
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.error is not None and "PolicyViolation" in outcome.error


# --------------------------------------------------------------------------
# 観点に無い段階
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("returned", "expected"), [(1, 0), (2, 3), (99, 3)])
def test_a_level_the_criterion_lacks_moves_to_the_nearest(returned: int, expected: int) -> None:
    judge, _ = _judge(returned)
    outcome = judge.evaluate(_request())

    assert outcome.status is EvaluatorStatus.OK
    (score,) = outcome.scores
    assert score.level == expected
    assert score.score_ratio == _criterion().level_for(expected).score_ratio
    # 寄せる前の値は記録に残す。モデルが何を返したかを後から追えるように。
    assert outcome.raw_output["verdict"]["level"] == returned


def test_votes_are_counted_before_the_level_is_moved() -> None:
    """99 と 3 は寄せれば同じ段階だが、票は割れとして数える（現状の動作）。

    寄せた後で数える実装に変えると確信度が 1.0 になり、観点に無い段階を
    返すほど不安定な応答が人の確認を経ずに通る。
    """
    judge, _ = _judge(99, 3)
    outcome = judge.evaluate(_request())

    (score,) = outcome.scores
    assert score.level == 3
    assert score.confidence == 0.5
    assert outcome.raw_output["agreement"] == 0.5


# --------------------------------------------------------------------------
# ollama に送るスキーマ
# --------------------------------------------------------------------------


def test_the_verdict_schema_survives_simplification() -> None:
    """`$defs` / `$ref` / `maxLength` は落ち、欄の名前と required は残る（ADR 0004）。"""
    simplified = simplify_schema(Verdict.model_json_schema())
    text = repr(simplified)

    assert "$defs" not in text
    assert "$ref" not in text
    assert "maxLength" not in text
    assert set(simplified["properties"]) == {  # type: ignore[call-overload]
        "observation",
        "level",
        "evidence",
        "rationale",
    }
    assert simplified["required"] == ["observation", "level", "evidence", "rationale"]
    span = simplified["properties"]["evidence"]["items"]  # type: ignore[index]
    assert set(span["properties"]) == {"start_line", "end_line", "quote"}
    assert span["required"] == ["start_line", "end_line"]
