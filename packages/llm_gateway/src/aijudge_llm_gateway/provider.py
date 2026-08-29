"""プロバイダ抽象と実装。

ollama / vLLM / クラウド API を同じ形で扱う。
差し替えがアプリ改修を伴わないことが目的（設計原則 P7）。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

from .types import (
    EmbeddingRequest,
    EmbeddingResponse,
    LlmError,
    LlmRequest,
    LlmResponse,
    ProviderCapabilities,
    Usage,
)

# 文法が保証するのは「形」だけ。値の制約は pydantic の検証に任せる。
#
# ollama の文法コンパイラは pydantic が出すスキーマをそのままでは扱えない。
# 実測（ollama 0.32.13）で確認した挙動:
#   - `$defs` / `$ref` があると 400 failed to parse grammar
#   - `maxLength: 2000` で 400（値によって通ったり通らなかったりする）
# 制約を落としてもモデルが構造を外すことはなく、範囲違反は Gateway の
# 検証と再試行が受け止める。構造に関わる `enum` だけは残す。
_STRUCTURAL_KEYS = frozenset({"type", "properties", "required", "items", "enum", "anyOf", "oneOf"})


def simplify_schema(schema: dict[str, object]) -> dict[str, object]:
    """プロバイダの文法コンパイラが受け付ける形にスキーマを削る。

    `$ref` を展開し、構造に関わらないキーワードを落とす。
    落とした制約は Gateway 側の pydantic 検証で担保される。
    """
    defs = schema.get("$defs")
    definitions = defs if isinstance(defs, dict) else {}

    def resolve(node: object, depth: int) -> object:
        if depth > 8:
            # 自己参照モデル。展開しきれないので素の object にする。
            return {"type": "object"}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = definitions.get(ref.removeprefix("#/$defs/"))
                if isinstance(target, dict):
                    return resolve(target, depth + 1)
                return {"type": "object"}
            simplified: dict[str, object] = {}
            for key, value in node.items():
                if key not in _STRUCTURAL_KEYS:
                    continue
                if key == "properties" and isinstance(value, dict):
                    # ここのキーはプロパティ名。フィルタしてはならない。
                    simplified[key] = {
                        name: resolve(subschema, depth) for name, subschema in value.items()
                    }
                elif key in ("required", "enum"):
                    simplified[key] = value
                else:
                    simplified[key] = resolve(value, depth)
            return simplified
        if isinstance(node, list):
            return [resolve(item, depth) for item in node]
        return node

    resolved = resolve(schema, 0)
    return resolved if isinstance(resolved, dict) else schema


@runtime_checkable
class Provider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def complete(self, request: LlmRequest) -> LlmResponse: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """埋め込みを出せるプロバイダ。

    **`Provider` と分けてある。** 埋め込みモデルを持たない構成は普通に
    あるので、必須にすると生成しかしない環境でプロバイダが書けなくなる。
    Gateway 側は持っているかどうかを見て、無ければはっきり断る。
    """

    name: str
    capabilities: ProviderCapabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class OllamaProvider:
    """ollama のネイティブ `/api/chat` を使う。

    OpenAI 互換の `/v1/chat/completions` ではなくネイティブ API を使うのは、
    思考の有無（`think`）とスキーマ制約（`format`）を確実に指定できるため。
    vLLM に移るときは OpenAI 互換の別プロバイダを足す。

    `constrained_decoding` を True にしていても**信用しない**。
    同じ ollama でもランナー（llama.cpp / MLX）によって `format` が
    無視されることを実測で確認している。検証と再試行は Gateway 側の責務。
    """

    def __init__(
        self,
        base_url: str = "http://slab-llm:11434",
        *,
        name: str = "slab-llm",
        local: bool = True,
        constrained_decoding: bool = True,
        vision: bool = True,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self.capabilities = ProviderCapabilities(
            constrained_decoding=constrained_decoding,
            vision=vision,
            local=local,
        )

    def complete(self, request: LlmRequest) -> LlmResponse:
        body: dict[str, object] = {
            "model": request.model,
            "messages": [message.model_dump() for message in request.messages],
            "think": request.thinking,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.json_schema is not None and self.capabilities.constrained_decoding:
            body["format"] = simplify_schema(request.json_schema)

        started = time.monotonic()
        payload = json.dumps(body).encode()
        http_request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # 本文にプロバイダ側の理由が入っている。捨てると原因が追えない。
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise LlmError(f"{self.name}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LlmError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        message = data.get("message") or {}
        return LlmResponse(
            text=message.get("content", ""),
            model=data.get("model", request.model),
            usage=Usage(
                prompt_tokens=int(data.get("prompt_eval_count") or 0),
                completion_tokens=int(data.get("eval_count") or 0),
                duration_ms=duration_ms,
            ),
            finish_reason=data.get("done_reason"),
        )

    def list_models(self) -> tuple[str, ...]:
        """導入済みモデルを列挙する。起動時の設定検証に使う。"""
        try:
            with urllib.request.urlopen(f"{self._base_url}/api/tags", timeout=10) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LlmError(f"{self.name}: cannot list models: {exc}") from exc
        return tuple(sorted(model["name"] for model in data.get("models", [])))


    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """ollama の `/api/embed`。

        **1 回の要求でまとめて渡す。** 課題を 1 件ずつ呼ぶと、既存 200 件との
        突き合わせが 200 往復になる。
        """
        started = time.monotonic()
        payload = json.dumps({"model": request.model, "input": list(request.texts)}).encode()
        http_request = urllib.request.Request(
            f"{self._base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise LlmError(f"{self.name}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LlmError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(request.texts):
            raise LlmError(f"{self.name}: 埋め込みの数が要求と合いません")
        return EmbeddingResponse(
            vectors=tuple(tuple(float(v) for v in vector) for vector in vectors),
            model=request.model,
            usage=Usage(duration_ms=int((time.monotonic() - started) * 1000)),
        )


class ScriptedProvider:
    """テスト用。決められた応答を順に返す。

    LLM を呼ばずに Gateway と評価器の挙動を検証するために使う。
    CI はネットワークに出ないので、既定の経路はこちら。
    """

    def __init__(
        self,
        responses: list[str],
        *,
        name: str = "scripted",
        local: bool = True,
        constrained_decoding: bool = False,
    ) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(
            constrained_decoding=constrained_decoding, local=local
        )
        self._responses = list(responses)
        self.calls: list[LlmRequest] = []
        self.embed_calls: list[EmbeddingRequest] = []

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """決定的な擬似埋め込み。

        **同じ文には同じベクトル、違う文には違うベクトル**を返せば、
        類似度の経路は試験できる。意味の近さは再現しないので、
        「言い換えを捉える」ことの試験にはならない ── そこは実機で測る。
        """
        self.embed_calls.append(request)
        vectors = []
        for text in request.texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append(tuple(byte / 255.0 for byte in digest[:16]))
        return EmbeddingResponse(vectors=tuple(vectors), model=request.model)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        if not self._responses:
            raise LlmError("scripted provider ran out of responses")
        return LlmResponse(
            text=self._responses.pop(0),
            model=request.model,
            usage=Usage(duration_ms=1),
        )
