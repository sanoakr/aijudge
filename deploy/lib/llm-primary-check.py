"""プライマリ LLM が「確定モデル」を実際に提供しているかを確認する。

なぜ必要か: `FallbackProvider` は `LlmError` を捕まえて必ずセカンダリへ回すので、
プライマリが落ちていても**採点は成功し続ける** ── つまり動いていることが障害を
隠す。2026-09-10 に実際にそれが起きた（主系が確定モデルを出せず、すべての採点が
従系で行われていた）。

判定は「応答したか」ではなく **どちらのプロバイダが応答したか**で行う。
`provider_name` が primary 以外なら、機能はしていてもプライマリは死んでいる。

学習者データは送らない（`DataClass.NON_PERSONAL` の固定プロンプト・設計原則 P7）。

運用機では `/usr/local/lib/aijudge/llm-primary-check.py` に置き、
`aijudge-llm-primary-check.sh` から呼ぶ。
"""

import os
import sys

from pydantic import BaseModel

from aijudge_llm_gateway import (
    ENV_FALLBACK_BASE_URL,
    LlmGateway,
    OllamaProvider,
    default_gateway,
    default_model,
)
from aijudge_llm_gateway.gateway import PromptTemplate
from aijudge_llm_gateway.types import DataClass

PROMPT = PromptTemplate(
    name="ops-primary-check",
    version="1",
    template="1+1 の答えを value に入れて JSON で返してください。",
)


class Answer(BaseModel):
    value: int


# 終了コード（sh 側が状態としてそのまま持つ・#426）。
# 0 = 主系も従系も応答 / 1 = 主系不能・従系で稼働 / 2 = 両系不能 / 3 = 主系は応答、従系が不能
FALLBACK_DOWN = 3


def _ask(gateway: LlmGateway, model: str) -> Answer:
    return gateway.complete_structured(
        PROMPT,
        Answer,
        model=model,
        data_class=DataClass.NON_PERSONAL,
        max_tokens=64,
        timeout_seconds=90,
    ).value


def _fallback_problem(model: str) -> str | None:
    """従系を**直接**叩く。主系が生きている間は、誰も従系を試さない（#426）。

    落ちていても主系が生きている限り採点は続くので、主系が落ちた瞬間に
    初めて「従系も駄目だった」と分かる ── 両系停止の直前まで気づけない。
    """
    url = os.environ.get(ENV_FALLBACK_BASE_URL)
    if not url:
        return None
    try:
        _ask(LlmGateway(OllamaProvider(url, name="fallback")), model)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    model = default_model()
    gateway = default_gateway()
    try:
        result = gateway.complete_structured(
            PROMPT,
            Answer,
            model=model,
            data_class=DataClass.NON_PERSONAL,
            max_tokens=64,
            timeout_seconds=90,
        )
    except Exception as exc:  # プライマリもセカンダリも駄目な場合
        print(f"NG both providers failed for {model}: {type(exc).__name__}: {exc}")
        return 2
    who = gateway.provider_name
    if who != "primary":
        print(f"NG primary cannot serve {model}; answered by {who!r} (value={result.value.value})")
        return 1
    problem = _fallback_problem(model)
    if problem is not None:
        print(f"NG primary serves {model} but the fallback does not: {problem}")
        return FALLBACK_DOWN
    print(f"OK primary serves {model} (value={result.value.value}); fallback answers too")
    return 0


if __name__ == "__main__":
    sys.exit(main())
