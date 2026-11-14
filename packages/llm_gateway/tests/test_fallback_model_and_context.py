"""従系に落ちたときのモデル名と、文脈長の送り方（2026-11-13・観点ごとの重さ）。

重いモデルは主系（PAIR）と従系（slab-llm）で置けるものが違う。主系と同じ名前の
まま従系へ回すと「モデルが無い」で落ち、主系が落ちている間は重さを名指しした
観点がすべて未採点になる。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from pydantic import BaseModel

from aijudge_llm_gateway import (
    ChatMessage,
    DataClass,
    FallbackProvider,
    LlmError,
    LlmGateway,
    LlmRequest,
    OllamaProvider,
    PromptTemplate,
    ProviderCapabilities,
    ScriptedProvider,
)


class _Down:
    name = "primary"
    capabilities = ProviderCapabilities(local=True)

    def complete(self, request: LlmRequest) -> None:
        raise LlmError("primary: connection refused")


def _request(**kwargs: Any) -> LlmRequest:
    return LlmRequest(
        messages=(ChatMessage(role="user", content="hi"),), model="qwen3.6:35b-a3b", **kwargs
    )


def test_the_secondary_is_called_with_the_fallback_model() -> None:
    secondary = ScriptedProvider(["ok"], name="secondary")
    response = FallbackProvider(_Down(), secondary).complete(
        _request(fallback_model="qwen3.8:27b-mlx")
    )
    assert secondary.calls[0].model == "qwen3.8:27b-mlx"
    # 実際に答えたモデルが記録に残る（P8）
    assert response.model == "qwen3.8:27b-mlx"


def test_without_a_fallback_model_the_secondary_gets_the_same_name() -> None:
    secondary = ScriptedProvider(["ok"], name="secondary")
    FallbackProvider(_Down(), secondary).complete(_request())
    assert secondary.calls[0].model == "qwen3.6:35b-a3b"


def test_the_primary_ignores_the_fallback_model() -> None:
    primary = ScriptedProvider(["ok"], name="primary")
    FallbackProvider(primary, ScriptedProvider([], name="secondary")).complete(
        _request(fallback_model="qwen3.8:27b-mlx")
    )
    assert primary.calls[0].model == "qwen3.6:35b-a3b"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        bodies.append(json.loads(request.data))
        reply = {"model": "m", "message": {"role": "assistant", "content": "ok"}}
        return _FakeResponse(json.dumps(reply).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return bodies


def test_num_ctx_is_sent_only_when_given(sent: list[dict[str, Any]]) -> None:
    """送らなければサーバの既定（運用は OLLAMA_CONTEXT_LENGTH=32768）のまま。"""
    OllamaProvider("http://host").complete(_request())
    OllamaProvider("http://host").complete(_request(num_ctx=16384))
    assert "num_ctx" not in sent[0]["options"]
    assert sent[1]["options"]["num_ctx"] == 16384


class _Answer(BaseModel):
    ok: bool


def test_the_gateway_passes_both_through() -> None:
    primary = ScriptedProvider([], name="primary")
    secondary = ScriptedProvider(['{"ok": true}'], name="secondary")
    gateway = LlmGateway(FallbackProvider(primary, secondary))
    prompt = PromptTemplate(name="p", version="1", template="u")
    result = gateway.complete_structured(
        prompt,
        _Answer,
        model="qwen3.6:35b-a3b",
        fallback_model="qwen3.8:27b-mlx",
        num_ctx=16384,
        data_class=DataClass.PERSONAL,
    )
    assert result.model_id == "qwen3.8:27b-mlx"
    assert secondary.calls[0].num_ctx == 16384
