"""観点が名指した重さのモデルで呼ぶ（2026-11-13）。

レポートの項目の有無を gemma4:e4b に読ませるのは重すぎた。観点の `judge_model` を
パイプラインが解決して `EvaluationRequest.judge_model` に入れ、評価器はそれで呼ぶ。
"""

from __future__ import annotations

import json

from aijudge_eval_checklist_ai_judge import ChecklistAiJudge
from test_checklist_ai_judge import _found, _item_case, _request

from aijudge_grading import JudgeModel
from aijudge_llm_gateway import LlmGateway
from aijudge_llm_gateway.provider import ScriptedProvider


def _calls(request):
    provider = ScriptedProvider([json.dumps(_found("目的"), ensure_ascii=False)])
    ChecklistAiJudge(LlmGateway(provider), model="gemma4:e4b", samples=1).evaluate(request)
    return provider.calls


def test_without_a_weight_the_default_model_is_used() -> None:
    (call,) = _calls(_request(cases=(_item_case("目的"),)))
    assert call.model == "gemma4:e4b"
    assert call.fallback_model is None
    assert call.num_ctx is None


def test_the_named_weight_decides_the_model() -> None:
    request = _request(cases=(_item_case("目的"),)).model_copy(
        update={
            "judge_model": JudgeModel(
                model="qwen3.6:35b-a3b", fallback_model="qwen3.8:27b-mlx", num_ctx=16384
            )
        }
    )
    (call,) = _calls(request)
    assert call.model == "qwen3.6:35b-a3b"
    assert call.fallback_model == "qwen3.8:27b-mlx"
    assert call.num_ctx == 16384
