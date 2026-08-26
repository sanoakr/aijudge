"""LLM ゲートウェイ（S6）。

責務は 4 つ。

1. **ポリシールーティング（P7）** — 学習者データを含むプロンプトは
   ローカルプロバイダにしか流さない。設定ミスは例外にする。
2. **構造化出力の保証（P4）** — スキーマ制約は当てにできない（実測で、
   同じ ollama でも MLX ランナーは `format` を無視した）。
   受け取った文字列から JSON を取り出し、pydantic で検証し、
   失敗したら誤りを添えて再試行する。
3. **バージョン記録（P8）** — どのプロンプト版・どのモデルで出したかを
   呼び出し側に返す。GradingContext に載せて再現可能にするため。
4. **自己一貫性** — 同じ問いを複数回サンプリングし、一致度を確信度にする。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .provider import Provider
from .types import (
    ChatMessage,
    DataClass,
    LlmRequest,
    PolicyViolation,
    StructuredOutputError,
    Usage,
)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """モデル出力から JSON 本体を取り出す。

    制約デコードが効かないプロバイダは、コードフェンスや前置きを付けてくる。
    ここを緩めに実装しておかないと、内容は正しいのに形式で落ちる。
    """
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group("body")
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text

    # 最初に現れる釣り合いの取れたオブジェクトを拾う。
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : index + 1]
    return text


class PromptTemplate(BaseModel):
    """バージョン付きプロンプト。

    版を付けるのは再現性のため（P8）。文面を直したら必ず版を上げる。
    版を上げずに文面を変えると、過去の採点が何で出たか分からなくなる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    system: str | None = None
    template: str = Field(min_length=1)

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def render(self, **values: object) -> tuple[ChatMessage, ...]:
        body = self.template.format(**values)
        messages: list[ChatMessage] = []
        if self.system:
            messages.append(ChatMessage(role="system", content=self.system))
        messages.append(ChatMessage(role="user", content=body))
        return tuple(messages)


class StructuredResult[TModel: BaseModel](BaseModel):
    """構造化呼び出しの結果と、再現に必要な出所。"""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    value: TModel
    model_id: str
    prompt_id: str
    provider: str
    attempts: int
    usage: Usage
    # 自己一貫性で複数回サンプリングしたときの、採用値への一致割合。
    agreement: float = 1.0
    samples: int = 1


class LlmGateway:
    """プロバイダの前段。アプリはここしか呼ばない。"""

    def __init__(self, provider: Provider, *, max_attempts: int = 3) -> None:
        self._provider = provider
        self._max_attempts = max_attempts

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def _check_policy(self, data_class: DataClass) -> None:
        if data_class is DataClass.PERSONAL and not self._provider.capabilities.local:
            raise PolicyViolation(
                f"refusing to send learner data to non-local provider "
                f"{self._provider.name!r}; see design principle P7"
            )

    def complete_structured[TModel: BaseModel](
        self,
        prompt: PromptTemplate,
        schema: type[TModel],
        *,
        model: str,
        data_class: DataClass,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout_seconds: float = 120.0,
        **values: object,
    ) -> StructuredResult[TModel]:
        """スキーマに合う応答を得るまで、上限回数まで再試行する。"""
        self._check_policy(data_class)

        messages = list(prompt.render(**values))
        json_schema = schema.model_json_schema()
        prompt_tokens = completion_tokens = duration_ms = 0
        last_error = ""

        for attempt in range(1, self._max_attempts + 1):
            response = self._provider.complete(
                LlmRequest(
                    messages=tuple(messages),
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_schema=json_schema,
                    timeout_seconds=timeout_seconds,
                )
            )
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
            duration_ms += response.usage.duration_ms

            try:
                value = schema.model_validate_json(extract_json(response.text))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                if attempt == self._max_attempts:
                    break
                # 誤りを具体的に伝えて直させる。やり直しを頼むだけでは直らない。
                messages.append(ChatMessage(role="assistant", content=response.text))
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            "その出力はスキーマに合いません。次のエラーを直し、"
                            "JSON オブジェクトだけを出力してください。\n"
                            f"エラー: {last_error[:1500]}\n"
                            f"スキーマ: {json.dumps(json_schema, ensure_ascii=False)}"
                        ),
                    )
                )
                continue

            return StructuredResult(
                value=value,
                model_id=response.model,
                prompt_id=prompt.id,
                provider=self._provider.name,
                attempts=attempt,
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=duration_ms,
                ),
            )

        raise StructuredOutputError(
            f"{self._provider.name}/{model} did not produce output matching "
            f"{schema.__name__} in {self._max_attempts} attempts: {last_error[:500]}"
        )

    def sample_structured[TModel: BaseModel](
        self,
        prompt: PromptTemplate,
        schema: type[TModel],
        *,
        model: str,
        data_class: DataClass,
        samples: int = 1,
        temperature: float = 0.0,
        key: str,
        **kwargs: object,
    ) -> StructuredResult[TModel]:
        """同じ問いを複数回サンプリングし、多数決と一致度を返す（自己一貫性）。

        `key` は一致を判定する属性名（採点なら段階）。
        一致度をそのまま確信度に使い、割れたら人間のレビューに回す（P5）。
        温度 0 で 1 回だけ引いても「自信があるか」は分からないので、
        複数回引いてばらつきを見る。
        """
        if samples < 1:
            raise ValueError("samples must be at least 1")

        results: list[StructuredResult[TModel]] = []
        for index in range(samples):
            results.append(
                self.complete_structured(
                    prompt,
                    schema,
                    model=model,
                    data_class=data_class,
                    # 1 回目は決定的に、2 回目以降はばらつきを見るために温度を上げる。
                    temperature=temperature if index == 0 else max(temperature, 0.7),
                    **kwargs,  # type: ignore[arg-type]
                )
            )

        votes = Counter(getattr(result.value, key) for result in results)
        winner, count = votes.most_common(1)[0]
        chosen = next(result for result in results if getattr(result.value, key) == winner)

        return chosen.model_copy(
            update={
                "agreement": count / len(results),
                "samples": len(results),
                "attempts": sum(result.attempts for result in results),
                "usage": Usage(
                    prompt_tokens=sum(r.usage.prompt_tokens for r in results),
                    completion_tokens=sum(r.usage.completion_tokens for r in results),
                    duration_ms=sum(r.usage.duration_ms for r in results),
                ),
            }
        )


def votes_of(values: Sequence[object]) -> dict[object, int]:
    """テストと可視化のための小さなヘルパ。"""
    return dict(Counter(values))
