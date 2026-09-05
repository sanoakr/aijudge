"""FallbackProvider の規則をテストで固定する。

プライマリが落ちている間だけセカンダリに回ることと、
P7（フォールバック先も学習者データを受け取ってよい前提でなければならない）を守る。
"""

from __future__ import annotations

import pytest

from aijudge_llm_gateway import (
    ChatMessage,
    EmbeddingRequest,
    FallbackProvider,
    LlmError,
    LlmRequest,
    ProviderCapabilities,
    ScriptedProvider,
)

REQUEST = LlmRequest(messages=(ChatMessage(role="user", content="hi"),), model="m")


class _DownProvider:
    """常に接続に失敗するプロバイダ（プライマリの障害を模す）。"""

    def __init__(self, name: str, *, local: bool = True) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(local=local)
        self.calls = 0

    def complete(self, request: LlmRequest) -> None:
        self.calls += 1
        raise LlmError(f"{self.name}: connection refused")

    def embed(self, request: EmbeddingRequest) -> None:
        self.calls += 1
        raise LlmError(f"{self.name}: connection refused")


def test_the_primary_is_used_while_it_answers() -> None:
    primary = ScriptedProvider(["primary says hi"], name="primary")
    secondary = _DownProvider("secondary")
    provider = FallbackProvider(primary, secondary)

    response = provider.complete(REQUEST)

    assert response.text == "primary says hi"
    assert provider.name == "primary"
    assert secondary.calls == 0


def test_a_failed_primary_falls_back_to_the_secondary() -> None:
    primary = _DownProvider("primary")
    secondary = ScriptedProvider(["secondary says hi"], name="secondary")
    provider = FallbackProvider(primary, secondary)

    response = provider.complete(REQUEST)

    assert response.text == "secondary says hi"
    # 実際に応答した側の名前に更新されている（P8: 採点したのはどちらか）。
    assert provider.name == "secondary"
    assert primary.calls == 1


def test_the_provider_reverts_to_primary_once_it_recovers() -> None:
    primary = ScriptedProvider(["primary is back"], name="primary")
    secondary = ScriptedProvider(["should not be used"], name="secondary")
    provider = FallbackProvider(primary, secondary)

    response = provider.complete(REQUEST)

    assert response.text == "primary is back"
    assert provider.name == "primary"


def test_embedding_calls_fall_back_too() -> None:
    primary = _DownProvider("primary")
    secondary = ScriptedProvider(["unused"], name="secondary")
    provider = FallbackProvider(primary, secondary)

    response = provider.embed(EmbeddingRequest(texts=("t",), model="m"))

    assert primary.calls == 1
    assert provider.name == "secondary"
    assert len(response.vectors) == 1


def test_a_fallback_to_a_non_local_provider_is_rejected_up_front() -> None:
    """フォールバック先が学外 API だと、プライマリが落ちた瞬間に学習者データが
    学外へ流れる経路になる（P7）。組み立て時に拒否する。
    """
    primary = ScriptedProvider(["x"], name="primary", local=True)
    cloud = ScriptedProvider(["x"], name="cloud", local=False)

    with pytest.raises(ValueError, match="P7"):
        FallbackProvider(primary, cloud)
