"""観点が名指した重さのモデルで呼ぶ（2026-11-13）。

コメントの説明を読む観点（prog2 exam08 p2 など）は gemma4:e4b には重すぎた。
観点の `judge_model` をパイプラインが解決し、評価器はそのモデルで呼ぶ。
"""

from __future__ import annotations

from test_source_selection import _request, _verdict_json

from aijudge_core import ArtifactKind
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grading import JudgeModel
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

SUBMISSION = "# 最小値を探す\nint min = a[0];\n".encode()


def _calls(request):
    provider = ScriptedProvider([_verdict_json(3)])
    outcome = RubricAiJudge(LlmGateway(provider), model="gemma4:e4b", samples=1).evaluate(request)
    return provider.calls, outcome


def test_without_a_weight_the_default_model_is_used() -> None:
    calls, outcome = _calls(_request(ArtifactKind.CODE, SUBMISSION))
    assert calls[0].model == "gemma4:e4b"
    assert calls[0].fallback_model is None
    assert outcome.model_id == "gemma4:e4b"


def test_the_named_weight_decides_the_model_and_is_recorded() -> None:
    request = _request(ArtifactKind.CODE, SUBMISSION).model_copy(
        update={"judge_model": JudgeModel(model="qwen3.6:35b-a3b")}
    )
    calls, outcome = _calls(request)
    assert calls[0].model == "qwen3.6:35b-a3b"
    # 実際に答えたモデルが記録に残る（P8）
    assert outcome.model_id == "qwen3.6:35b-a3b"
