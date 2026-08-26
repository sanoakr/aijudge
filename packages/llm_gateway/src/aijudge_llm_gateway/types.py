"""LLM 呼び出しの語彙。

アプリはプロバイダを直接呼ばない（設計原則 P7）。ここで定義した型だけを使い、
モデルの差し替えがアプリ改修を伴わないようにする。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DataClass(StrEnum):
    """プロンプトに含まれるデータの機微度。

    学習者の解答は個人に紐づく学習履歴そのものなので PERSONAL。
    問題文だけなら NON_PERSONAL。この区別がルーティングを決める。
    """

    PERSONAL = "personal"
    NON_PERSONAL = "non_personal"


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class ProviderCapabilities(BaseModel):
    """プロバイダができること。

    `constrained_decoding` が重要。ollama の MLX ランナーのように
    JSON スキーマを渡しても無視する構成が実在するため、
    「スキーマを渡せば必ず従う」を前提にしてはならない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    constrained_decoding: bool = False
    vision: bool = False
    # 学習者データを送ってよいか。学外 API はここが False。
    local: bool = False


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


class LlmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, gt=0)
    # JSON スキーマ。プロバイダが対応していれば制約デコードに使う。
    json_schema: dict[str, object] | None = None
    timeout_seconds: float = Field(default=120.0, gt=0.0)
    # 思考モデルの内部推論。採点では出力形式を安定させたいので既定は無効。
    thinking: bool = False


class LlmResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    usage: Usage = Usage()
    finish_reason: str | None = None


class LlmError(Exception):
    """プロバイダ呼び出しに失敗した。"""


class PolicyViolation(LlmError):
    """データ機微度とプロバイダの組み合わせがポリシーに反する。"""


class StructuredOutputError(LlmError):
    """規定回数の再試行でもスキーマに合う出力を得られなかった。"""
