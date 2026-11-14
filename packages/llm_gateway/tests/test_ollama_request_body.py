"""ollama に送る要求本文と、応答の終了理由の読み方を固定する（#690）。

`test_ollama_wire.py` は画像の鍵と content / thinking の読み分けだけを見る。
ここでは次の 3 つを固める。どれも崩れても `ScriptedProvider` を使う試験は
通ってしまう ── 実プロバイダの組み立てと読み方を通らないからである。

- **`done_reason` を `finish_reason` に読む。** 読まないと本番で
  `OutputTruncated` が一度も出なくなり、ADR 0021 の打ち切り検出と
  `image_text` の `repeat_penalty` 付き再試行が黙って無効になる。
- **`format` には削ったスキーマを送る**（`simplify_schema`）。`$defs` / `$ref`
  や `maxLength` が残ると ollama が HTTP 400 `failed to parse grammar` を
  返し、AI 評価が全件 FAILED になる（ADR 0004）。
- **`think` と `num_predict`** は要求のとおりに送る。

ネットワークには出ない。`urlopen` を差し替えて本文を捕まえる。
"""

from __future__ import annotations

import io
import json
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from aijudge_llm_gateway import ChatMessage, LlmRequest, OllamaProvider
from aijudge_llm_gateway.provider import simplify_schema

OUTPUT_BUDGET = 321


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """送った本文を捕まえ、決めた応答を返す。"""
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        seen["body"] = json.loads(request.data)
        return _FakeResponse(json.dumps(seen["reply"]).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    seen["reply"] = {"model": "m", "message": {"role": "assistant", "content": "ok"}}
    return seen


class _Span(BaseModel):
    start_line: int = Field(ge=1)
    quote: str = Field(default="", max_length=2000)


class _Answer(BaseModel):
    """`$defs` / `$ref`・`maxLength`・`enum` をすべて含むスキーマを作るための型。"""

    verdict: Literal["yes", "no"]
    note: str = Field(min_length=1, max_length=500)
    spans: list[_Span] = Field(min_length=1)


def _request(**kwargs: Any) -> LlmRequest:
    return LlmRequest(messages=(ChatMessage(role="user", content="hi"),), model="m", **kwargs)


def _keys_anywhere(node: object) -> set[str]:
    """入れ子のどこかに現れる辞書の鍵をすべて集める（プロパティ名も含む）。"""
    if isinstance(node, dict):
        found = set(node)
        for value in node.values():
            found |= _keys_anywhere(value)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= _keys_anywhere(item)
        return found
    return set()


# --------------------------------------------------------------------------
# 応答の終了理由
# --------------------------------------------------------------------------


def test_done_reason_length_is_read_as_a_truncation(capture: dict[str, Any]) -> None:
    """ollama は終了理由を `done_reason` で返す。OpenAI 互換の名前ではない。"""
    capture["reply"] = {
        "model": "m",
        "message": {"role": "assistant", "content": '{"level": '},
        "done_reason": "length",
    }
    assert OllamaProvider("http://host").complete(_request()).finish_reason == "length"


def test_a_normal_stop_is_not_read_as_a_truncation(capture: dict[str, Any]) -> None:
    capture["reply"] = {
        "model": "m",
        "message": {"role": "assistant", "content": "{}"},
        "done_reason": "stop",
    }
    assert OllamaProvider("http://host").complete(_request()).finish_reason == "stop"


# --------------------------------------------------------------------------
# 要求本文
# --------------------------------------------------------------------------


def test_thinking_and_the_output_budget_are_sent_as_requested(capture: dict[str, Any]) -> None:
    """`think` を落とすと ollama 側の既定（モデルによっては思考あり）になる。"""
    OllamaProvider("http://host").complete(_request(max_tokens=OUTPUT_BUDGET))

    assert capture["body"]["think"] is False
    assert capture["body"]["options"]["num_predict"] == OUTPUT_BUDGET
    assert capture["body"]["stream"] is False


def test_thinking_true_is_passed_through(capture: dict[str, Any]) -> None:
    OllamaProvider("http://host").complete(_request(thinking=True))
    assert capture["body"]["think"] is True


def test_the_schema_sent_as_format_is_simplified(capture: dict[str, Any]) -> None:
    """`$defs` / `$ref` / `maxLength` を残すと ollama は 400 を返す（ADR 0004）。"""
    schema = _Answer.model_json_schema()
    assert "$defs" in schema, "前提: 元のスキーマは $defs を持つ"

    OllamaProvider("http://host").complete(_request(json_schema=schema))

    sent = capture["body"]["format"]
    assert sent == simplify_schema(schema)
    keys = _keys_anywhere(sent)
    assert not keys & {"$defs", "$ref", "maxLength", "minLength", "minimum", "title"}
    # 構造は残る。プロパティ名・required・enum は文法に要る。
    assert set(sent["properties"]) == {"verdict", "note", "spans"}
    assert sent["required"] == ["verdict", "note", "spans"]
    assert sent["properties"]["verdict"]["enum"] == ["yes", "no"]
    # `$ref` は展開されて中身が入っている。
    item = sent["properties"]["spans"]["items"]
    assert item["type"] == "object"
    assert set(item["properties"]) == {"start_line", "quote"}
    assert item["required"] == ["start_line"]


def test_no_format_is_sent_without_constrained_decoding(capture: dict[str, Any]) -> None:
    """文法制約を持たない構成では `format` を付けない（検証は Gateway が担う）。"""
    provider = OllamaProvider("http://host", constrained_decoding=False)
    provider.complete(_request(json_schema=_Answer.model_json_schema()))
    assert "format" not in capture["body"]


def test_a_property_named_like_a_dropped_keyword_survives() -> None:
    """プロパティ名は鍵として削らない。`title` という名前の欄でも残る。"""

    class _Titled(BaseModel):
        title: str = Field(max_length=10)

    simplified = simplify_schema(_Titled.model_json_schema())
    assert set(simplified["properties"]) == {"title"}  # type: ignore[call-overload]
