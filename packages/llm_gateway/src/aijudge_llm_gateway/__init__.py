"""aiJudge LLM gateway (S6)。

アプリは LLM プロバイダを直接呼ばない（設計原則 P7）。
ここが機微度によるルーティング、構造化出力の検証と再試行、
プロンプト／モデルのバージョン記録、自己一貫性サンプリングを引き受ける。
"""

from __future__ import annotations

import os

from .gateway import (
    LlmGateway,
    PromptTemplate,
    StructuredResult,
    extract_json,
)
from .provider import (
    EmbeddingProvider,
    FallbackProvider,
    OllamaProvider,
    Provider,
    ScriptedProvider,
)
from .types import (
    ChatMessage,
    DataClass,
    EmbeddingRequest,
    EmbeddingResponse,
    LlmError,
    LlmRequest,
    LlmResponse,
    PolicyViolation,
    ProviderCapabilities,
    StructuredOutputError,
    Usage,
)

# 既定はローカルの ollama。**学外のホストを既定にしない。** 個人情報を
# 学外へ出さないのが前提条件（設計原則 P7）で、既定値はそれを破らない側に
# 倒しておく ── 設定を忘れた環境が黙って外へ送るより、繋がらない方がよい。
# 学内 GPU ホストを使うなら `AIJUDGE_LLM_BASE_URL` で指すこと。
DEFAULT_BASE_URL = "http://localhost:11434"
# gemma4:e4b を既定にしているのは実測による。同ホストの qwen3.8:27b-mlx は
# MLX ランナーのため JSON スキーマ制約が効かず、長い日本語プロンプトで
# 構造化出力が安定しなかった。より大きいモデルが常に良いとは限らない。
DEFAULT_MODEL = "gemma4:e4b"

ENV_BASE_URL = "AIJUDGE_LLM_BASE_URL"
ENV_MODEL = "AIJUDGE_LLM_MODEL"
# 未設定なら平常運転（フォールバックなし）。プライマリが落ちている間だけ
# ここが指すホストに切り替える。**学外を指してはならない**（P7）。
ENV_FALLBACK_BASE_URL = "AIJUDGE_LLM_FALLBACK_BASE_URL"


def default_model() -> str:
    return os.environ.get(ENV_MODEL, DEFAULT_MODEL)


def default_gateway() -> LlmGateway:
    """環境変数で上書き可能な既定ゲートウェイ。

    `AIJUDGE_LLM_FALLBACK_BASE_URL` を設定すると、プライマリが応答しない間だけ
    そちらに切り替える（`FallbackProvider` 参照）。
    """
    primary = OllamaProvider(os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL), name="primary")
    fallback_url = os.environ.get(ENV_FALLBACK_BASE_URL)
    if not fallback_url:
        return LlmGateway(primary)
    secondary = OllamaProvider(fallback_url, name="fallback")
    return LlmGateway(FallbackProvider(primary, secondary))


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "ENV_BASE_URL",
    "ENV_FALLBACK_BASE_URL",
    "ENV_MODEL",
    "ChatMessage",
    "DataClass",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "FallbackProvider",
    "LlmError",
    "LlmGateway",
    "LlmRequest",
    "LlmResponse",
    "OllamaProvider",
    "PolicyViolation",
    "PromptTemplate",
    "Provider",
    "ProviderCapabilities",
    "ScriptedProvider",
    "StructuredOutputError",
    "StructuredResult",
    "Usage",
    "default_gateway",
    "default_model",
    "extract_json",
]
