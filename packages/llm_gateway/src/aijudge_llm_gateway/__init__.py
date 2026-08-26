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
from .provider import OllamaProvider, Provider, ScriptedProvider
from .types import (
    ChatMessage,
    DataClass,
    LlmError,
    LlmRequest,
    LlmResponse,
    PolicyViolation,
    ProviderCapabilities,
    StructuredOutputError,
    Usage,
)

# 既定は学内 GPU ホスト。個人情報を学外へ出さないため（前提条件）。
DEFAULT_BASE_URL = "http://slab-llm:11434"
# gemma4:e4b を既定にしているのは実測による。同ホストの qwen3.8:27b-mlx は
# MLX ランナーのため JSON スキーマ制約が効かず、長い日本語プロンプトで
# 構造化出力が安定しなかった。より大きいモデルが常に良いとは限らない。
DEFAULT_MODEL = "gemma4:e4b"

ENV_BASE_URL = "AIJUDGE_LLM_BASE_URL"
ENV_MODEL = "AIJUDGE_LLM_MODEL"


def default_model() -> str:
    return os.environ.get(ENV_MODEL, DEFAULT_MODEL)


def default_gateway() -> LlmGateway:
    """環境変数で上書き可能な既定ゲートウェイ。"""
    return LlmGateway(OllamaProvider(os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)))


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "ChatMessage",
    "DataClass",
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
