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
    #: 添付する画像（base64 で符号化した PNG / JPEG のそのままのバイト列）。
    #:
    #: **本文と同じ型に載せる。** 画像用の呼び出しを別に作ると、ポリシー
    #: 検査・スキーマ検証・再試行・プロンプト版の記録を 2 回書くことになり、
    #: 片方が必ず遅れる。載せる先はメッセージなので、再試行で会話が伸びても
    #: 画像は最初の発言に付いたまま動かない。
    #:
    #: 画像を渡せるかはプロバイダの性質（`ProviderCapabilities.vision`）で、
    #: 渡せない相手に渡すのは設定の誤りである。Gateway が呼ぶ前に断る。
    images: tuple[str, ...] = ()


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


class EmbeddingRequest(BaseModel):
    """埋め込みの要求。

    **生成とは別の型にする。** 温度も最大トークン数もスキーマも意味を持たず、
    同じ型に載せると「埋め込みに温度を渡す」経路ができる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    texts: tuple[str, ...] = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0)


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vectors: tuple[tuple[float, ...], ...]
    model: str
    usage: Usage = Usage()


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


class CapabilityMismatch(LlmError):
    """プロバイダができないことを要求した（画像を見られないモデルへの画像など）。

    **`PolicyViolation` と分ける。** 片方は「送ってはいけない相手に送ろうとした」
    で、直し方は経路を変えることである。こちらは「送ってもよいが相手が読めない」
    で、直し方はモデルを変えることである。同じ例外にすると、運用機で
    `gemma4:e4b`（vision なし）に画像を渡した設定ミスが、個人情報の流出未遂と
    同じ文言で報告される。
    """


class StructuredOutputError(LlmError):
    """規定回数の再試行でもスキーマに合う出力を得られなかった。"""


class OutputTruncated(LlmError):
    """出力が上限に達して切れた。**やり直しても同じところで切れる。**

    `StructuredOutputError` と分ける。あちらは「モデルが形を外した」で、
    誤りを伝えれば直ることがある。こちらは**予算が足りなかった**ので、
    同じ予算で言い直させても必ず同じ結果になる。

    分けていなかったときに何が起きたか（2026-09-19 の実測）── 画像の
    書き起こしが縮退ループに入って上限まで生成し、壊れた JSON が返った。
    Gateway はそれを形の誤りと見なし、**3,666 文字の壊れた本文を会話に
    足してから**「直してください」と言う。プロンプトが伸びるので次はより
    早く切れる。3 回とも切れて失敗し、その間ずっと計算資源を使う。

    自動で予算を上げてやり直すことはしない。**普通に長い出力と、抜けない
    ループを、切れた事実だけからは区別できない**（実測: 上限 8,000 では
    8,000 トークン生成しきって 225 秒かかった。生成速度は 35〜40 tok/s で、
    上限を上げた分だけ失敗が遅くなる）。切れたら諦めて人へ回す ── それが
    この製品の既定の振る舞いである（P5）。
    """
