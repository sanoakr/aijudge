"""ollama へ実際に送る形と、受け取った応答の読み方を固定する。

ネットワークには出ない。`urlopen` を差し替えて、組み立てた要求本文と、
返ってきた JSON の読み方だけを見る。ここが崩れると、症状は「モデルが
おかしい」の形で現れて原因に辿り着けない（実際にそうなった ── 下の
`thinking` の件）。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from aijudge_llm_gateway import ChatMessage, LlmRequest, OllamaProvider


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


def _request(**kwargs: Any) -> LlmRequest:
    return LlmRequest(messages=(ChatMessage(role="user", content="hi", **kwargs),), model="m")


def test_images_are_sent_as_the_images_field(capture: dict[str, Any]) -> None:
    OllamaProvider("http://host").complete(_request(images=("QUJD", "REVG")))
    assert capture["body"]["messages"][0]["images"] == ["QUJD", "REVG"]


def test_a_message_without_images_does_not_carry_the_key(capture: dict[str, Any]) -> None:
    """空の `images` を送らない。画像を扱わないモデルに渡す意味が無い。"""
    OllamaProvider("http://host").complete(_request())
    assert "images" not in capture["body"]["messages"][0]


def test_the_answer_is_read_from_thinking_when_content_is_empty(
    capture: dict[str, Any],
) -> None:
    """**qwen3-vl は `format` を渡すと答えを `thinking` に入れる。**

    `think: false` を指定しても起き、画像の有無とも無関係（2026-09-19 の
    実測。同じホストの `gemma4:e4b` では起きない）。`content` だけを読むと
    応答は返っているのに空文字になり、Gateway が 3 回再試行して必ず失敗する
    ── 実測で 17 件すべてがそうなった。
    """
    capture["reply"] = {
        "model": "m",
        "message": {"role": "assistant", "content": "", "thinking": '{"level": 2}'},
    }
    response = OllamaProvider("http://host").complete(_request())
    assert response.text == '{"level": 2}'


def test_content_wins_when_both_are_present(capture: dict[str, Any]) -> None:
    """`thinking` は空のときの代わりであって、優先はしない。

    思考を出すモデルでは `thinking` に下書きが入り `content` に答えが入る。
    逆にすると、答えではなく途中の考えを採点記録に残すことになる。
    """
    capture["reply"] = {
        "model": "m",
        "message": {"role": "assistant", "content": "答え", "thinking": "下書き"},
    }
    assert OllamaProvider("http://host").complete(_request()).text == "答え"
