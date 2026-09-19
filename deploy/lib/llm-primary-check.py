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

import sys

from pydantic import BaseModel

from aijudge_llm_gateway import default_gateway, default_model
from aijudge_llm_gateway.gateway import PromptTemplate
from aijudge_llm_gateway.types import DataClass

PROMPT = PromptTemplate(
    name="ops-primary-check",
    version="1",
    template="1+1 の答えを value に入れて JSON で返してください。",
)


class Answer(BaseModel):
    value: int


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
    print(f"OK primary serves {model} (value={result.value.value})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
