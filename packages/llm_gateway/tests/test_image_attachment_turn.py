"""画像は「テンプレートの利用者の発言」に 1 度だけ付くことを固定する（#690）。

`test_gateway.py` の `test_the_image_stays_on_the_first_turn_when_the_schema_fails`
は、最後の検査が `... or with_images[0].role == "user"` で、再試行で足される
訂正依頼も `role="user"` なので後半が常に真になる。添付を「送信のたびに最後の
発言へ付け替える」実装に変えても通ってしまう。

付け替えると画像は訂正依頼の発言へ移り、モデルは「この画像」と言う最初の問いを
画像なしで読むことになる。ここでは添付先を発言の位置で固める。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aijudge_llm_gateway import DataClass, LlmGateway, PromptTemplate, ScriptedProvider

IMAGES = ("QUJD",)


class _Verdict(BaseModel):
    level: int = Field(ge=0, le=3)
    rationale: str = Field(min_length=1)


def _retried_messages(prompt: PromptTemplate) -> list:
    provider = ScriptedProvider(
        ["これは JSON ではありません", '{"level": 2, "rationale": "ok"}'],
        vision=True,
    )
    LlmGateway(provider).complete_structured(
        prompt,
        _Verdict,
        model="m",
        data_class=DataClass.PERSONAL,
        images=IMAGES,
        thing="この画像",
    )
    assert len(provider.calls) == 2, "前提: 1 回目はスキーマに合わず再試行している"
    return list(provider.calls[1].messages)


def test_the_image_stays_on_the_first_turn_without_a_system_prompt() -> None:
    prompt = PromptTemplate(name="t", version="1", template="judge {thing}")
    retried = _retried_messages(prompt)

    assert retried[0].role == "user"
    assert retried[0].images == IMAGES
    assert all(not message.images for message in retried[1:])


def test_the_image_goes_to_the_template_user_turn_after_a_system_prompt() -> None:
    """system 付きのテンプレートでは 2 番目（テンプレートの user 発言）に付く。"""
    prompt = PromptTemplate(name="t", version="1", system="採点者です。", template="judge {thing}")
    retried = _retried_messages(prompt)

    assert retried[0].role == "system"
    assert not retried[0].images
    assert retried[1].role == "user"
    assert "この画像" in retried[1].content
    assert retried[1].images == IMAGES
    assert all(not message.images for message in retried[2:])
