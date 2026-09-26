"""aiJudge LLM gateway (S6)。

アプリは LLM プロバイダを直接呼ばない（設計原則 P7）。
ここが機微度によるルーティング、構造化出力の検証と再試行、
プロンプト／モデルのバージョン記録、自己一貫性サンプリングを引き受ける。
"""

from __future__ import annotations

import logging
import os

from .gateway import (
    LlmGateway,
    PromptTemplate,
    StructuredResult,
    extract_json,
)
from .injection import instruction_lines, instruction_notice, submission_boundary
from .locality import ENV_LOCAL_DOMAINS, is_local_url
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
    OutputTruncated,
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
# 画像経路の従系。**主系と同じく、落ちている間だけ回す**（#341）。
ENV_VISION_FALLBACK_BASE_URL = "AIJUDGE_LLM_VISION_FALLBACK_BASE_URL"
# 未設定なら平常運転（フォールバックなし）。プライマリが落ちている間だけ
# ここが指すホストに切り替える。**学外を指してはならない**（P7）。
ENV_FALLBACK_BASE_URL = "AIJUDGE_LLM_FALLBACK_BASE_URL"


logger = logging.getLogger(__name__)


def default_model() -> str:
    return os.environ.get(ENV_MODEL, DEFAULT_MODEL)


def default_gateway() -> LlmGateway:
    """環境変数で上書き可能な既定ゲートウェイ。

    `AIJUDGE_LLM_FALLBACK_BASE_URL` を設定すると、プライマリが応答しない間だけ
    そちらに切り替える（`FallbackProvider` 参照）。
    """
    primary = OllamaProvider(os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL), name="primary")
    return LlmGateway(_with_fallback(primary, os.environ.get(ENV_FALLBACK_BASE_URL), "fallback"))


def _with_fallback(primary: OllamaProvider, fallback_url: str | None, name: str) -> Provider:
    """従系を足す。**学内と認められない従系は足さない**（P7・#415）。

    足すと `FallbackProvider` が構成の時点で例外を出し、ワーカーが起動
    できなくなる ── 決定的評価まで止まる（P2）。従系を落として主系だけで
    動かし、理由を記録する。直すのは `AIJUDGE_LLM_LOCAL_DOMAINS` の設定である。
    """
    if not fallback_url:
        return primary
    secondary = OllamaProvider(fallback_url, name=name)
    if secondary.capabilities.local != primary.capabilities.local:
        logger.error(
            "LLM fallback %s is not a permitted local host (set %s); running without it",
            fallback_url,
            ENV_LOCAL_DOMAINS,
        )
        return primary
    return FallbackProvider(primary, secondary)


def default_vision_model() -> str:
    """画像を読むモデル。未設定なら `DEFAULT_VISION_MODEL`。

    **通常のモデル設定（`AIJUDGE_LLM_MODEL`）へは落とさない。** 落とすと、
    vision を持たないモデルが既定で画像を受け取ることになる ── 断られるなら
    まだよいが、プロバイダによっては画像を黙って捨てて本文だけで答える。
    """
    return os.environ.get(ENV_VISION_MODEL, DEFAULT_VISION_MODEL)


def default_vision_gateway() -> LlmGateway:
    """画像を渡す呼び出し用のゲートウェイ。

    `AIJUDGE_LLM_VISION_BASE_URL` が主系、`AIJUDGE_LLM_VISION_FALLBACK_BASE_URL`
    があれば従系（#341）。どちらも未設定なら通常の経路をそのまま使う。

    **かつてフォールバックを持たせていなかった**（ADR 0021 §3）。理由は
    「従系に vision がある保証が無く、画像を読めない相手に回すと、断られるか
    （よい方）根拠のない答えが返る（悪い方）」である。いまは
    `FallbackProvider` が**両者の共通部分**を能力として名乗るので、従系が
    vision を持たなければ合成した側も持たず、画像を渡す呼び出しは
    `CapabilityMismatch` で呼ぶ前に落ちる ── 危ない方（黙って本文だけで
    答える）には倒れない。

    **ただし「名乗り」までしか見ていない。** 指したモデルが実際に画像を
    読めるかは `OllamaProvider.model_capabilities` で実測すること。
    proxy の後ろに複数ノードがある構成では、それでもまだ足りない。
    """
    vision_url = os.environ.get(ENV_VISION_BASE_URL)
    if not vision_url:
        return default_gateway()
    primary = OllamaProvider(vision_url, name="vision")
    return LlmGateway(
        _with_fallback(primary, os.environ.get(ENV_VISION_FALLBACK_BASE_URL), "vision-fallback")
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_VISION_MODEL",
    "ENV_BASE_URL",
    "ENV_FALLBACK_BASE_URL",
    "ENV_MODEL",
    "ENV_VISION_BASE_URL",
    "ENV_VISION_FALLBACK_BASE_URL",
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
    "OutputTruncated",
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
    "instruction_lines",
    "instruction_notice",
    "is_local_url",
    "submission_boundary",
]
