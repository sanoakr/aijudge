"""テストが本物の LLM に届かないことを固定する（#616）。

ルートの `conftest.py` が、`AIJUDGE_LIVE_LLM=1` のとき以外は LLM の向き先を
閉じたポートに差し替える。これが外れると、手元で LLM が動いている開発機で
`pytest` が生成を待ち続ける（2026-10-15 に終わらなくなった）。
"""

from __future__ import annotations

import os

import pytest

live = os.environ.get("AIJUDGE_LIVE_LLM") == "1"


@pytest.mark.skipif(live, reason="AIJUDGE_LIVE_LLM=1 のときは本物の LLM を使う")
def test_the_llm_points_at_a_closed_port() -> None:
    assert os.environ["AIJUDGE_LLM_BASE_URL"] == "http://127.0.0.1:9"
    assert os.environ["AIJUDGE_LLM_VISION_BASE_URL"] == "http://127.0.0.1:9"


@pytest.mark.skipif(live, reason="AIJUDGE_LIVE_LLM=1 のときは本物の LLM を使う")
def test_no_fallback_llm_is_configured() -> None:
    """従系が残ると、主系が拒否されたあとにそちらへ回って生成を待つ。"""
    assert "AIJUDGE_LLM_FALLBACK_BASE_URL" not in os.environ
    assert "AIJUDGE_LLM_VISION_FALLBACK_BASE_URL" not in os.environ


@pytest.mark.skipif(live, reason="AIJUDGE_LIVE_LLM=1 のときは本物の LLM を使う")
def test_the_default_gateway_follows_the_closed_port() -> None:
    from aijudge_llm_gateway import default_gateway

    # 従系が無いので、gateway の下は主系の provider 1 つだけになる
    provider = default_gateway()._provider
    assert provider._base_url == "http://127.0.0.1:9"
