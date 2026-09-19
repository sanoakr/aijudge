"""FallbackProvider の規則をテストで固定する。

プライマリが落ちている間だけセカンダリに回ることと、
P7（フォールバック先も学習者データを受け取ってよい前提でなければならない）を守る。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

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


# --------------------------------------------------------------------------
# できることは両者の共通部分（#341）
# --------------------------------------------------------------------------


def test_the_pair_can_only_do_what_both_can_do() -> None:
    """**主系の能力をそのまま名乗らない。**

    どちらが応答するかは呼んでみるまで決まらないので、「主系ならできる」は
    保証にならない。画像でこれが効く ── vision を持たない従系に画像が渡ると、
    モデルは画像を無視して本文だけで答える。応答は返り、スキーマにも合い、
    内容だけが根拠のない作り話になる。
    """
    seeing = ScriptedProvider(["{}"], name="primary", vision=True)
    blind = ScriptedProvider(["{}"], name="secondary", vision=False)

    assert FallbackProvider(seeing, blind).capabilities.vision is False
    assert FallbackProvider(seeing, seeing).capabilities.vision is True


def test_constrained_decoding_is_combined_the_same_way() -> None:
    """同じ理由。片方が文法を効かせられないなら、組にして当てにはできない。"""
    strict = ScriptedProvider(["{}"], name="p", constrained_decoding=True)
    loose = ScriptedProvider(["{}"], name="s", constrained_decoding=False)

    assert FallbackProvider(strict, loose).capabilities.constrained_decoding is False


def test_an_image_is_refused_when_the_fallback_cannot_read_it() -> None:
    """合成した側が vision を名乗らないので、Gateway が呼ぶ前に断る。

    **危ない方には倒れない** ── 主系が生きている間だけ通って、落ちた瞬間に
    根拠のない答えが混ざる、という壊れ方をしない。
    """
    from aijudge_llm_gateway import (
        CapabilityMismatch,
        DataClass,
        LlmGateway,
        PromptTemplate,
    )

    seeing = ScriptedProvider(['{"ok": true}'], name="primary", vision=True)
    blind = ScriptedProvider(['{"ok": true}'], name="secondary", vision=False)
    gateway = LlmGateway(FallbackProvider(seeing, blind))

    class _Answer(BaseModel):
        ok: bool

    with pytest.raises(CapabilityMismatch):
        gateway.complete_structured(
            PromptTemplate(name="t", version="1", template="read {thing}"),
            _Answer,
            model="m",
            data_class=DataClass.PERSONAL,
            images=("QUJD",),
            thing="this",
        )
    assert seeing.calls == [], "断ったのにプロバイダを呼んでいる"
