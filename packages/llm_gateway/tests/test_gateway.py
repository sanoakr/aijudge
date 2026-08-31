"""ゲートウェイの規則をテストで固定する。

ネットワークには出ない。実 LLM を叩く検証は evals/test_llm_live.py（任意実行）。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PolicyViolation,
    PromptTemplate,
    ProviderCapabilities,
    ScriptedProvider,
    StructuredOutputError,
    extract_json,
)


class Verdict(BaseModel):
    level: int = Field(ge=0, le=3)
    rationale: str = Field(min_length=1)


PROMPT = PromptTemplate(name="test_prompt", version="1", template="judge {thing}")


# --------------------------------------------------------------------------
# JSON の取り出し — 制約デコードが効かないプロバイダへの備え
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"level": 2, "rationale": "ok"}',
        '```json\n{"level": 2, "rationale": "ok"}\n```',
        '```\n{"level": 2, "rationale": "ok"}\n```',
        'はい、評価しました。\n{"level": 2, "rationale": "ok"}\n以上です。',
        '{"level": 2, "rationale": "ok"}\n\n**補足**: 蛇足',
    ],
)
def test_json_is_recovered_from_chatty_output(raw: str) -> None:
    """前置き・コードフェンス・後書きが付いても中身を拾う。

    緩く実装しないと、内容は正しいのに形式だけで落ちる。
    """
    assert Verdict.model_validate_json(extract_json(raw)).level == 2


def test_braces_inside_strings_do_not_confuse_the_extractor() -> None:
    raw = 'ここです → {"level": 1, "rationale": "printf(\\"{}\\") が怪しい"}'
    assert Verdict.model_validate_json(extract_json(raw)).level == 1


def test_a_code_fence_inside_the_json_is_not_mistaken_for_the_wrapper() -> None:
    """**コードを返す構造化出力を壊さない。**

    以前はフェンスを本文のどこでも拾っていたので、JSON 文字列の中の
    ```c を外側の囲みと取り違え、その中身（C のソース）を JSON として
    読もうとして落ちた。**モデルが素直にコードをフェンスで囲むほど確実に
    壊れる**という形で、参照解答を返す生成は 3 回の再試行も毎回同じところで
    落ちていた（#52）。
    """
    import json

    payload = json.dumps(
        {"level": 2, "rationale": "参照解答:\n```c\nint main(void){return 0;}\n```"},
        ensure_ascii=False,
    )
    assert Verdict.model_validate_json(extract_json(payload)).level == 2
    assert extract_json(payload) == payload


# --------------------------------------------------------------------------
# ポリシールーティング（設計原則 P7）
# --------------------------------------------------------------------------


def test_learner_data_never_reaches_a_non_local_provider() -> None:
    cloud = ScriptedProvider(['{"level": 3, "rationale": "x"}'], name="cloud", local=False)
    gateway = LlmGateway(cloud)

    with pytest.raises(PolicyViolation, match="non-local provider"):
        gateway.complete_structured(
            PROMPT, Verdict, model="m", data_class=DataClass.PERSONAL, thing="code"
        )
    # 呼ばれてすらいないこと。判定の前に送ってしまっては意味がない。
    assert cloud.calls == []


def test_non_personal_prompts_may_use_a_cloud_provider() -> None:
    cloud = ScriptedProvider(['{"level": 3, "rationale": "x"}'], name="cloud", local=False)
    result = LlmGateway(cloud).complete_structured(
        PROMPT, Verdict, model="m", data_class=DataClass.NON_PERSONAL, thing="a task statement"
    )
    assert result.value.level == 3


# --------------------------------------------------------------------------
# 構造化出力の検証と再試行（設計原則 P4）
# --------------------------------------------------------------------------


def test_a_malformed_response_is_repaired_on_retry() -> None:
    provider = ScriptedProvider(
        [
            "すみません、JSON ではありません。",
            '{"level": 9, "rationale": "範囲外"}',  # スキーマ違反（le=3）
            '{"level": 2, "rationale": "三回目で合った"}',
        ]
    )
    result = LlmGateway(provider).complete_structured(
        PROMPT, Verdict, model="m", data_class=DataClass.PERSONAL, thing="code"
    )

    assert result.value.level == 2
    assert result.attempts == 3
    # 再試行では「やり直して」ではなく、具体的なエラーを添えて直させる。
    assert "エラー" in provider.calls[-1].messages[-1].content


def test_giving_up_raises_rather_than_inventing_a_score() -> None:
    provider = ScriptedProvider(["だめ", "やはりだめ", "まだだめ"])
    with pytest.raises(StructuredOutputError, match="Verdict"):
        LlmGateway(provider).complete_structured(
            PROMPT, Verdict, model="m", data_class=DataClass.PERSONAL, thing="code"
        )


def test_the_schema_is_passed_only_to_providers_that_honour_it() -> None:
    """制約デコード非対応のプロバイダにスキーマを渡しても意味がない。"""
    plain = ScriptedProvider(['{"level": 1, "rationale": "x"}'], constrained_decoding=False)
    LlmGateway(plain).complete_structured(
        PROMPT, Verdict, model="m", data_class=DataClass.PERSONAL, thing="code"
    )
    # リクエストにはスキーマが載る。実際に使うかはプロバイダ実装の判断。
    assert plain.calls[0].json_schema is not None
    assert plain.capabilities == ProviderCapabilities(constrained_decoding=False, local=True)


# --------------------------------------------------------------------------
# 出所とバージョン（設計原則 P8）
# --------------------------------------------------------------------------


def test_the_result_carries_the_prompt_version() -> None:
    provider = ScriptedProvider(['{"level": 1, "rationale": "x"}'])
    result = LlmGateway(provider).complete_structured(
        PROMPT, Verdict, model="qwen", data_class=DataClass.PERSONAL, thing="code"
    )
    assert result.prompt_id == "test_prompt@1"
    assert result.model_id == "qwen"


# --------------------------------------------------------------------------
# 自己一貫性
# --------------------------------------------------------------------------


def test_unanimous_samples_give_full_confidence() -> None:
    provider = ScriptedProvider([f'{{"level": 2, "rationale": "r{i}"}}' for i in range(3)])
    result = LlmGateway(provider).sample_structured(
        PROMPT,
        Verdict,
        model="m",
        data_class=DataClass.PERSONAL,
        samples=3,
        key="level",
        thing="code",
    )
    assert result.value.level == 2
    assert result.agreement == pytest.approx(1.0)
    assert result.samples == 3


def test_a_split_vote_lowers_confidence_and_takes_the_majority() -> None:
    provider = ScriptedProvider(
        [
            '{"level": 2, "rationale": "a"}',
            '{"level": 3, "rationale": "b"}',
            '{"level": 2, "rationale": "c"}',
        ]
    )
    result = LlmGateway(provider).sample_structured(
        PROMPT,
        Verdict,
        model="m",
        data_class=DataClass.PERSONAL,
        samples=3,
        key="level",
        thing="code",
    )
    assert result.value.level == 2
    assert result.agreement == pytest.approx(2 / 3)


def test_the_first_sample_is_deterministic_and_later_ones_vary() -> None:
    """温度 0 で 1 回引くだけでは「自信があるか」が分からないため。"""
    provider = ScriptedProvider([f'{{"level": 1, "rationale": "r{i}"}}' for i in range(3)])
    LlmGateway(provider).sample_structured(
        PROMPT,
        Verdict,
        model="m",
        data_class=DataClass.PERSONAL,
        samples=3,
        key="level",
        thing="code",
    )
    temperatures = [call.temperature for call in provider.calls]
    assert temperatures[0] == 0.0
    assert all(temperature > 0.0 for temperature in temperatures[1:])
