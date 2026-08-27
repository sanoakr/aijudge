"""フィードバック生成器の組み立て。

LLM のプロバイダを決めるのは合成の中心（app 層）の仕事。S6 が使えない
環境ではゲートウェイを渡さず、要約へ落ちる（設計原則 P2）。
"""

from __future__ import annotations

import os

from aijudge_feedback import FeedbackGenerator
from aijudge_llm_gateway import LlmGateway

ENV_FEEDBACK_MODEL = "AIJUDGE_FEEDBACK_MODEL"


def build_feedback_generator(gateway: LlmGateway | None = None) -> FeedbackGenerator:
    """既定のフィードバック生成器。

    モデル名が設定されていなければ LLM を使わない要約になる。無言にはしない
    （無言だと、学習者にはシステムが壊れたのか提出に問題が無いのか区別できない）。
    """
    model = os.environ.get(ENV_FEEDBACK_MODEL, "")
    if gateway is None or not model:
        return FeedbackGenerator()
    return FeedbackGenerator(gateway, model=model)
