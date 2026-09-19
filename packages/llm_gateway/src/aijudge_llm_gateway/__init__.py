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
    CapabilityMismatch,
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

# 画像を読むモデル。**主系と同じとは限らない。** 運用機の主系は
# `gemma4:e4b` で vision を持たないため、画像を使う評価器だけを別のホスト・
# 別のモデルへ回せるようにしてある。未設定なら通常の経路をそのまま使う
# （繋がらないより、設定が 1 か所で済む方が運用しやすい。画像を読めない
# 相手に渡そうとすれば Gateway が `CapabilityMismatch` で断る）。
DEFAULT_VISION_MODEL = "qwen3-vl:8b"

ENV_BASE_URL = "AIJUDGE_LLM_BASE_URL"
ENV_MODEL = "AIJUDGE_LLM_MODEL"
ENV_VISION_BASE_URL = "AIJUDGE_LLM_VISION_BASE_URL"
ENV_VISION_MODEL = "AIJUDGE_LLM_VISION_MODEL"
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


def default_vision_model() -> str:
    """画像を読むモデル。未設定なら `DEFAULT_VISION_MODEL`。

    **通常のモデル設定（`AIJUDGE_LLM_MODEL`）へは落とさない。** 落とすと、
    vision を持たないモデルが既定で画像を受け取ることになる ── 断られるなら
    まだよいが、プロバイダによっては画像を黙って捨てて本文だけで答える。
    """
    return os.environ.get(ENV_VISION_MODEL, DEFAULT_VISION_MODEL)


def default_vision_gateway() -> LlmGateway:
    """画像を渡す呼び出し用のゲートウェイ。

    `AIJUDGE_LLM_VISION_BASE_URL` があればそのホストだけを見る。**フォール
    バックは持たない** ── 主系・従系の両方に vision モデルがある保証は無く、
    落ちている間の代替が「画像を読めない相手」では、断られるか（よい方）
    根拠のない答えが返る（悪い方）。読めないときは採点せず人へ回す方が
    この製品の建て付けに合う（P5）。
    """
    vision_url = os.environ.get(ENV_VISION_BASE_URL)
    if not vision_url:
        return default_gateway()
    return LlmGateway(OllamaProvider(vision_url, name="vision"))


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_VISION_MODEL",
    "ENV_BASE_URL",
    "ENV_FALLBACK_BASE_URL",
    "ENV_MODEL",
    "ENV_VISION_BASE_URL",
    "ENV_VISION_MODEL",
    "CapabilityMismatch",
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
    "default_vision_gateway",
    "default_vision_model",
    "extract_json",
]
