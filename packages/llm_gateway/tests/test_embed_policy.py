"""埋め込みも生成と同じ P7 の検査を通ることを固定する（#690）。

`LlmGateway.embed` の docstring が書くとおり、埋め込みからは元の文が部分的に
復元できる。呼び出し元（`course_admin` の重複検出・内容の類似）は提出物の
本文を渡すので、学外の埋め込み API が設定されていたら、ここで断らなければ
本文がそのまま学外へ出る。

呼び出し側が `PERSONAL` を申告することは `apps/admin/tests/test_code_similarity.py`
が偽のゲートウェイで固めている。ここで固めるのは残りの半分、本物の
ゲートウェイが学外プロバイダを断ることである。
"""

from __future__ import annotations

import pytest

from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    LlmRequest,
    LlmResponse,
    PolicyViolation,
    ProviderCapabilities,
    ScriptedProvider,
)


def test_learner_text_is_not_embedded_by_a_remote_provider() -> None:
    provider = ScriptedProvider([], local=False)
    with pytest.raises(PolicyViolation):
        LlmGateway(provider).embed(("本文",), model="m", data_class=DataClass.PERSONAL)
    assert provider.embed_calls == [], "断ったのにプロバイダへ本文を渡している"


def test_non_personal_text_may_be_embedded_remotely() -> None:
    """課題文のような個人に紐づかない文は、学外でも埋め込める。"""
    provider = ScriptedProvider([], local=False)
    vectors = LlmGateway(provider).embed(("課題文",), model="m", data_class=DataClass.NON_PERSONAL)
    assert len(vectors) == 1
    assert len(provider.embed_calls) == 1


def test_learner_text_is_embedded_by_a_local_provider() -> None:
    provider = ScriptedProvider([], local=True)
    vectors = LlmGateway(provider).embed(
        ("本文", "別の本文"), model="m", data_class=DataClass.PERSONAL
    )
    assert len(vectors) == 2
    assert provider.embed_calls[0].texts == ("本文", "別の本文")


class _CompletionOnly:
    """生成しかできないプロバイダ（`embed` を持たない）。"""

    name = "completion-only"
    capabilities = ProviderCapabilities(local=True)

    def complete(self, request: LlmRequest) -> LlmResponse:  # pragma: no cover - 呼ばれない
        raise AssertionError("embed の試験で complete を呼んではならない")


def test_a_provider_without_embeddings_is_refused_clearly() -> None:
    """黙って空を返すと「似たものが 1 件も無い」と区別が付かない。"""
    with pytest.raises(LlmError) as raised:
        LlmGateway(_CompletionOnly()).embed(("本文",), model="m", data_class=DataClass.PERSONAL)
    assert not isinstance(raised.value, PolicyViolation)
